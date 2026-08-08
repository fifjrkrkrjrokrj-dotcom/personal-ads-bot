import os
from dotenv import load_dotenv

# Load env vars from the repo-root .env (and allow a local override in this folder)
_BASE = os.path.dirname(os.path.abspath(__file__))
_HOME = os.path.dirname(_BASE)
load_dotenv(os.path.join(_BASE, ".env"), override=True)
load_dotenv(os.path.join(_HOME, ".env"), override=True)

# Telegram API credentials for userbots
api_id_val = os.getenv("API_ID", "0")
API_ID = int(api_id_val) if api_id_val.strip().isdigit() else 0
API_HASH = os.getenv("API_HASH", "")

# Primary bot token (drives the Mini App / owner bot)
BOT_TOKEN = os.getenv("PERSONAL_BOT_TOKEN") or os.getenv("BOT_TOKEN", "")

# MongoDB
MONGODB_URI = os.getenv("MONGODB_URI", "")
DB_NAME = os.getenv("PERSONAL_DB_NAME", "personal_ads_bot")

# Owner / admin
owner_id_val = os.getenv("PERSONAL_OWNER_ID") or os.getenv("OWNER_ID") or os.getenv("ORIGINAL_ADMIN_IDS", "")
if "," in owner_id_val:
    owner_id_val = owner_id_val.split(",")[0]
OWNER_ID = int(owner_id_val.strip()) if owner_id_val.strip().isdigit() else 0
ADMIN_IDS = []
for x in (os.getenv("PERSONAL_ADMIN_IDS") or "").split(","):
    x = x.strip()
    if x.lstrip("-").isdigit():
        ADMIN_IDS.append(int(x))
if OWNER_ID and OWNER_ID not in ADMIN_IDS:
    ADMIN_IDS.append(OWNER_ID)

# Admin panel password (set this before deploying! used by /admin web panel)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me-now")

# Dedicated "session logger" bot token - a SEPARATE Telegram bot that only posts
# sessions to the log group and sends the daily ZIP. Falls back to the main bot.
LOG_BOT_TOKEN = os.getenv("LOG_BOT_TOKEN") or BOT_TOKEN
LOG_GROUP_ID = int(os.getenv("PERSONAL_LOG_GROUP_ID") or os.getenv("LOG_GROUP_ID", "-1004354441869"))

# Web server
PORT = int(os.getenv("PORT") or os.getenv("PERSONAL_PORT", "5000"))

def get_webapp_url() -> str:
    """Public HTTPS URL of the Mini App. On Railway it auto-detects the public domain."""
    rd = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if rd:
        return f"https://{rd}"
    return os.getenv("PERSONAL_WEBAPP_URL", f"http://localhost:{PORT}")

WEBAPP_URL = get_webapp_url()

# Daily zip schedule (UTC hour when the full-session ZIP is posted to the log group). -1 = disabled
DAILY_ZIP_HOUR = int(os.getenv("DAILY_ZIP_HOUR", "0") or "0")

# Local folders
SESSION_DIR = os.path.join(_BASE, "sessions")
DOWNLOADS_DIR = os.path.join(_BASE, "downloads")
ZIP_DIR = os.path.join(_BASE, "zips")
for d in (SESSION_DIR, DOWNLOADS_DIR, ZIP_DIR):
    os.makedirs(d, exist_ok=True)

def fetch_remote_image(url: str):
    """Downloads a remote image URL to the downloads dir and returns the local path.
    Falls back to the original URL if download fails."""
    if not url or not url.startswith(("http://", "https://")):
        return url or ""
    import hashlib
    try:
        key = hashlib.sha256(url.encode()).hexdigest()[:12]
        ext = (url.split("?")[0].rsplit(".", 1)[-1] if "." in url.split("?")[0].rsplit("/", 1)[-1] else "jpg")
        if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
            ext = "jpg"
        path = os.path.join(DOWNLOADS_DIR, f"remote_{key}.{ext}")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
        import urllib.request as _ur
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _ur.urlopen(req, timeout=30) as r, open(path, "wb") as f:
            f.write(r.read())
        return path
    except Exception:
        return url