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

    parts = [p for p in (emoji, text) if p]
    msg = "\n\n".join(parts) if parts else "👋"

    if event.sender_id == config.OWNER_ID:
        msg += "\n\n💡 _Admin: open the panel on the Mini App website or type /admin_"

    webapp_url = database.get_webapp_url()
    try:
        buttons = [[types.KeyboardButtonSimpleWebView(btn_text, f"{webapp_url}/")]]
        if start_image and os.path.exists(start_image):
            await event.respond(msg, buttons=buttons, parse_mode="html", file=start_image)
        else:
            await event.respond(msg, buttons=buttons, parse_mode="html")
    except Exception as e:
        logger.warning(f"webview button failed ({e}); sending plain")
        if start_image and os.path.exists(start_image):
            try:
                await event.respond(msg, parse_mode="html", file=start_image)
            except Exception:
                await event.respond(msg, parse_mode="html")
        else:
            await event.respond(msg, parse_mode="html")

def _register_handlers(client: TelegramClient):
    @client.on(events.NewMessage(pattern="^/start"))
    async def start_handler(event):
        if not event.is_private:
            return
        try:
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
            await event.reply("📞 **Contact mila!** Login OTP bhej raha hai... Mini App window me code dekho.")
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

        # Set Bot Menu (Mini App) button so the icon/button appears next to the input.
        webapp_url = database.get_webapp_url()
        try:
            await client(functions.bots.SetBotMenuButtonRequest(
                user_id=types.InputUserEmpty(),
                button=types.BotMenuButton(text="🚀 Mini App", url=f"{webapp_url}/")
            ))
        except Exception as e:
            logger.warning(f"Could not set menu button for bot {name}: {e}")

        _bots[token] = {"client": client, "task": None, "name": name or (me.username or me.id), "status": "running"}
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