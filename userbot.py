import os
import logging
import asyncio
import random
import re
from datetime import datetime, timedelta
from typing import Optional, List, Any
from telethon import TelegramClient
from telethon.errors import (
    PeerFloodError,
    FloodWaitError,
    SlowModeWaitError,
    UserBannedInChannelError,
    ChatWriteForbiddenError,
    ChannelPrivateError,
    ChannelInvalidError,
    ChatIdInvalidError,
    UserNotParticipantError
)
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.account import UpdateProfileRequest

import config
import database

logger = logging.getLogger(__name__)

def _resolve_photo(photo):
    """Return a local file path for a campaign photo (accepts local path or http(s) URL)."""
    if not photo:
        return None
    if isinstance(photo, str) and photo.startswith(("http://", "https://")):
        return config.fetch_remote_image(photo)
    if os.path.exists(photo):
        return photo
    return None

class UserBotSession:
    def __init__(self, phone: str):
        self.phone = phone
        self.client: Optional[TelegramClient] = None
        self.is_running = False
        self.broadcast_task: Optional[asyncio.Task] = None
        self.session_path = os.path.join(config.SESSION_DIR, f"{self.phone}.session")

    async def start(self) -> bool:
        if self.is_running:
            return True
            
        logger.info(f"Starting userbot session for phone: {self.phone}")
        db_record = database.get_userbot(self.phone)
        if not db_record:
            logger.error(f"No database record found for userbot: {self.phone}")
            return False
            
        # If the local session file does not exist, recreate it from DB bytes
        if not os.path.exists(self.session_path):
            session_bytes = db_record.get("session_bytes")
            if session_bytes:
                logger.info(f"Restoring session file from DB bytes for {self.phone}")
                with open(self.session_path, "wb") as f:
                    f.write(session_bytes)
            else:
                logger.error(f"No session bytes available in DB for {self.phone}")
                return False
                
        try:
            api_id, api_hash = database.get_next_api_credentials()
            self.client = TelegramClient(self.session_path, api_id, api_hash)
            await self.client.connect()
            
            if not await self.client.is_user_authorized():
                logger.error(f"Userbot session {self.phone} is unauthorized.")
                db_record["status"] = "unauthorized"
                db_record["last_error"] = "Session expired or revoked on Telegram."
                database.save_userbot(db_record)
                return False
                
            self.is_running = True
            db_record["status"] = "active"
            db_record["last_error"] = ""
            database.save_userbot(db_record)
            
            # Apply profile branding if configured by owner
            try:
                await self.apply_branding()
            except Exception as b_err:
                logger.warning(f"Failed to apply branding on startup for {self.phone}: {b_err}")
            
            # Start background broadcast loop
            self.broadcast_task = asyncio.create_task(self.broadcast_loop())
            logger.info(f"Successfully started userbot for {self.phone}")
            return True
        except Exception as e:
            logger.exception(f"Failed to start userbot {self.phone}: {e}")
            db_record["status"] = "error"
            db_record["last_error"] = str(e)
            database.save_userbot(db_record)
            return False

    async def stop(self):
        logger.info(f"Stopping userbot session for phone: {self.phone}")
        self.is_running = False
        if self.broadcast_task:
            self.broadcast_task.cancel()
            self.broadcast_task = None
            
        if self.client:
            # Try to restore profile branding before disconnecting
            try:
                await self.restore_branding()
            except Exception as b_err:
                logger.warning(f"Failed to restore profile branding for {self.phone}: {b_err}")
                
            try:
                await self.client.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting userbot client {self.phone}: {e}")
            self.client = None

    async def get_targets(self) -> List[Any]:
        """
        Fetches broadcast targets: groups (if owner wants groups) and/or
        every user the account has a private chat with (DMs).
        Returns a list of entities ready for send_message.
        """
        targets = []
        if not self.client:
            return targets
        settings = database.get_owner_settings()
        target_mode = settings.get("broadcast_target", "groups")
        try:
            groups_ok = target_mode in ("groups", "both")
            dm_ok = target_mode in ("dm", "both")
            async for dialog in self.client.iter_dialogs():
                if dialog.is_group or dialog.is_channel:
                    if groups_ok:
                        targets.append(dialog.entity)
                elif dialog.is_user:
                    if dm_ok:
                        targets.append(dialog.entity)
        except Exception as e:
            logger.error(f"Error fetching dialogs for {self.phone}: {e}")
        return targets

    async def get_groups(self) -> List[Any]:
        """
        Fetches all group and megagroup dialogs the account has access to.
        """
        groups = []
        if not self.client:
            return groups
            
        try:
            async for dialog in self.client.iter_dialogs():
                if dialog.is_group:
                    groups.append(dialog.entity)
        except Exception as e:
            logger.error(f"Error fetching dialogs for {self.phone}: {e}")
        return groups

    @staticmethod
    def _parse_schedule_times(schedule: str) -> List[str]:
        """
        Parses an owner/operator schedule like "09:30, 14:00, 21:45" into a valid HH:MM list.
        """
        times = []
        for part in re.split(r"[,\s;]+", schedule):
            part = part.strip()
            m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", part)
            if m:
                times.append(f"{int(m.group(1)):02d}:{m.group(2)}")
        return sorted(set(times))

    async def _sleep_until_schedule(self, times: List[str]):
        """
        Sleeps until the next configured broadcast time of the day.
        """
        now = datetime.now()
        now_str = f"{now.hour:02d}:{now.minute:02d}"
        upcoming = [t for t in times if t > now_str]
        if not upcoming:
            upcoming = times
        target_str = min(upcoming)
        target_hh, target_mm = map(int, target_str.split(":"))
        target = now.replace(hour=target_hh, minute=target_mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        delta = (target - now).total_seconds()
        logger.info(f"Userbot {self.phone} sleeping {int(delta)}s until broadcast @ {target_str}")
        slept = 0
        while slept < delta and self.is_running:
            await asyncio.sleep(min(5, delta - slept))
            slept += 5

    async def apply_branding(self):
        """
        Appends the owner-defined branding name and bio suffixes to the userbot profile.
        Stores original profile details inside userbot's database record to allow restoration.
        """
        if not self.client:
            return
            
        settings = database.get_owner_settings()
        brand_name_text = settings.get("branding_name_text")
        brand_bio_text = settings.get("branding_bio_text")
        
        if not brand_name_text and not brand_bio_text:
            return
            
        logger.info(f"Checking profile branding for userbot: {self.phone}")
        db_record = database.get_userbot(self.phone) or {}
        
        full_user = await self.client(GetFullUserRequest('me'))
        user_me = full_user.users[0]
        full_profile = full_user.full_user
        
        orig_first_name = user_me.first_name or ""
        orig_bio = full_profile.about or ""
        
        # Save original details in DB if not already stored
        db_modified = False
        if "original_name" not in db_record:
            db_record["original_name"] = orig_first_name
            db_modified = True
        if "original_bio" not in db_record:
            db_record["original_bio"] = orig_bio
            db_modified = True
            
        if db_modified:
            database.save_userbot(db_record)
            
        new_first_name = orig_first_name
        if brand_name_text and brand_name_text not in orig_first_name:
            new_first_name = (orig_first_name + " " + brand_name_text)[:64]
            
        new_bio = orig_bio
        if brand_bio_text and brand_bio_text not in orig_bio:
            new_bio = (orig_bio + " " + brand_bio_text)[:70]
            
        if new_first_name != orig_first_name or new_bio != orig_bio:
            logger.info(f"Applying branding to profile {self.phone}: Name='{new_first_name}', Bio='{new_bio}'")
            await self.client(UpdateProfileRequest(
                first_name=new_first_name,
                about=new_bio
            ))

    async def restore_branding(self):
        """
        Restores the userbot profile's original name and bio from the database.
        """
        if not self.client:
            return
            
        db_record = database.get_userbot(self.phone)
        if not db_record:
            return
            
        orig_name = db_record.get("original_name")
        orig_bio = db_record.get("original_bio")
        
        if orig_name is not None or orig_bio is not None:
            logger.info(f"Restoring original profile for userbot: {self.phone}")
            await self.client(UpdateProfileRequest(
                first_name=orig_name if orig_name else "User",
                about=orig_bio if orig_bio else ""
            ))

    async def broadcast_loop(self):
        """
        Periodically fetches the owner's ad settings and sends them to all groups.
        Supports HTML formatted text and rotating multiple image/caption campaigns.
        """
        await asyncio.sleep(5) # initial sleep to let things warm up
        while self.is_running:
            settings = database.get_owner_settings()
            if not settings.get("is_active"):
                logger.info(f"Broadcasting is paused globally by owner. Userbot {self.phone} waiting...")
                await asyncio.sleep(60)
                continue
                
            campaigns = settings.get("campaigns", [])
            campaigns = [c for c in campaigns if c.get("text") or c.get("photo")]
            
            if not campaigns:
                logger.info(f"No campaigns with text are configured. Userbot {self.phone} waiting...")
                await asyncio.sleep(60)
                continue
                
            logger.info(f"Userbot {self.phone} starting broadcast round...")
            targets = await self.get_targets()
            if not targets:
                logger.info(f"No broadcast targets for {self.phone}. Waiting...")
                for _ in range(0, 60, 5):
                    if not self.is_running:
                        break
                    await asyncio.sleep(5)
                continue
            sent_count = 0
            total_targets = len(targets)
            
            for target in targets:
                if not self.is_running:
                    break
                    
                # Re-verify settings inside loop
                settings = database.get_owner_settings()
                if not settings.get("is_active"):
                    break
                    
                # Select a random campaign from list
                campaign = random.choice(campaigns)
                ad_text = campaign.get("text", "")
                photo_path = campaign.get("photo")
                
                try:
                    # Parse spintax if present, then send
                    processed_msg = ad_text
                    if "{" in processed_msg and "}" in processed_msg:
                        def replace_spintax(match):
                            options = match.group(1).split("|")
                            return random.choice(options)
                        processed_msg = re.sub(r"\{([^}]+)\}", replace_spintax, processed_msg)
                        
                    # Check if campaign includes a photo and file exists locally
                    resolved_photo = _resolve_photo(photo_path)
                    if resolved_photo:
                        await self.client.send_message(
                            target.id,
                            processed_msg,
                            file=resolved_photo,
                            parse_mode='html'
                        )
                    else:
                        await self.client.send_message(
                            target.id,
                            processed_msg,
                            parse_mode='html'
                        )
                        
                    sent_count += 1
                    
                    # Random delay between group sends to avoid spam flags (e.g. 8-15 seconds)
                    delay = random.uniform(8.0, 15.0)
                    await asyncio.sleep(delay)
                    
                except PeerFloodError:
                    logger.warning(f"PeerFloodError on {self.phone}. Pausing this account for 1 hour.")
                    db_record = database.get_userbot(self.phone)
                    if db_record:
                        db_record["status"] = "restricted"
                        db_record["last_error"] = "Restricted by Telegram for spam (PeerFloodError)."
                        database.save_userbot(db_record)
                    await asyncio.sleep(3600) # wait an hour
                    break
                except FloodWaitError as fwe:
                    logger.warning(f"FloodWaitError on {self.phone}. Must wait {fwe.seconds} seconds.")
                    await asyncio.sleep(fwe.seconds + 5)
                except SlowModeWaitError as smwe:
                    await asyncio.sleep(smwe.seconds)
                except (UserBannedInChannelError, ChatWriteForbiddenError, ChannelPrivateError, ChannelInvalidError, ChatIdInvalidError, UserNotParticipantError):
                    # Skip groups we cannot post to
                    pass
                except Exception as e:
                    err_name = e.__class__.__name__
                    if err_name in ("UserDeactivatedError", "AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError"):
                        logger.error(f"Userbot {self.phone} auth session invalidated: {e}")
                        db_record = database.get_userbot(self.phone)
                        if db_record:
                            db_record["status"] = "unauthorized"
                            db_record["last_error"] = f"Session revoked: {e}"
                            database.save_userbot(db_record)
                        self.is_running = False
                        break
                    else:
                        logger.warning(f"Failed to send to target {target.id}: {e}")
                        
            # Update broadcast statistics
            if sent_count > 0:
                db_record = database.get_userbot(self.phone)
                if db_record:
                    db_record.setdefault("stats", {})
                    db_record["stats"]["broadcast_count"] = db_record["stats"].get("broadcast_count", 0) + sent_count
                    database.save_userbot(db_record)
                    
            # Get sleep interval from soft settings (default 5 minutes).
            # Prefer the owner-set "broadcast schedule" times when configured.
            interval = settings.get("interval", 300)
            schedule = settings.get("broadcast_schedule", "")
            if schedule:
                parsed = self._parse_schedule_times(schedule)
                if parsed:
                    logger.info(f"Userbot {self.phone} using daily schedule {parsed}")
                    await self._sleep_until_schedule(parsed)
                    if not self.is_running:
                        break
                    continue
            logger.info(f"Userbot {self.phone} finished broadcast round. Sleeping for {interval}s")
            # sleep in chunks of 5s to allow responsive stopping
            for _ in range(0, int(interval), 5):
                if not self.is_running:
                    break
                await asyncio.sleep(5)
