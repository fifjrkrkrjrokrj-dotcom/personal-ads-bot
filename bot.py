import os
import logging
import asyncio
import uuid
from typing import Optional, Dict, Any
from telethon import TelegramClient, events, Button
from telethon.tl import types
import config
import database
import manager
import web_server

logger = logging.getLogger(__name__)

# Global bot client instance
bot_client: Optional[TelegramClient] = None

# Admin states tracking for owner configuration updates
# Can be a string (simple inputs) or a dict (wizard flows)
_admin_states: Dict[int, Any] = {}

def register_bot_handlers(client: TelegramClient):
    global bot_client
    bot_client = client

    # Welcome / Start Command Handler
    @client.on(events.NewMessage(pattern="^/start"))
    async def start_handler(event):
        if not event.is_private:
            return
            
        user_id = event.sender_id
        settings = database.get_owner_settings()
        emoji = settings.get("start_emoji", "").strip()
        welcome_text = settings.get("start_text", "").strip()
        button_text = settings.get("start_button_text", "START ✅").strip()
        start_image = settings.get("start_image")

        parts = []
        if emoji:
            parts.append(emoji)
        if welcome_text:
            parts.append(welcome_text)
            
        combined_message = "\n\n".join(parts) if parts else "👋"
        
        # Add Owner tip if user is owner
        if user_id == config.OWNER_ID:
            combined_message += "\n\n💡 _Boss, you can type /admin to open the control panel._"
            
        try:
            buttons = [
                [types.KeyboardButtonSimpleWebView(button_text, f"{database.get_webapp_url()}/")]
            ]
            if start_image and os.path.exists(start_image):
                await event.respond(combined_message, buttons=buttons, parse_mode='html', file=start_image)
            else:
                await event.respond(combined_message, buttons=buttons, parse_mode='html')
        except Exception as e:
            logger.error(f"Failed to send welcome message with webview button: {e}")
            if start_image and os.path.exists(start_image):
                try:
                    await event.respond(combined_message, parse_mode='html', file=start_image)
                except Exception:
                    await event.respond(combined_message, parse_mode='html')
            else:
                await event.respond(combined_message, parse_mode='html')

    # Admin Command Handler
    @client.on(events.NewMessage(pattern="^/admin"))
    async def admin_command_handler(event):
        if not event.is_private:
            return
            
        user_id = event.sender_id
        if user_id != config.OWNER_ID:
            await event.respond("❌ You are not authorized to access this control panel.")
            return
            
        await send_admin_panel(event)

    # Admin Callback Queries (Buttons)
    @client.on(events.CallbackQuery(pattern="^admin_"))
    async def admin_callback_handler(event):
        user_id = event.sender_id
        if user_id != config.OWNER_ID:
            await event.answer("Unauthorised access.", alert=True)
            return
            
        data = event.data.decode("utf-8")
        
        # --- Main Panel ---
        if data == "admin_panel":
            _admin_states.pop(user_id, None) # clean any active wizard
            await send_admin_panel(event, edit=True)
            
        # --- Add Campaign Text Wizard ---
        elif data == "admin_add_campaign":
            _admin_states[user_id] = {"step": "WAITING_FOR_CAMPAIGN_TEXT"}
            await event.edit(
                "📝 **Please send the text for the new Ad Campaign.**\n\n"
                "HTML formatting is supported to make clickable hyperlinks.\n"
                "Example: `🚀 Join our <a href=\"https://t.me/channel\">Channel</a> for daily giveaways!`",
                buttons=[Button.inline("🔙 Cancel", "admin_panel")]
            )
            
        # --- Skip Campaign Photo Trigger ---
        elif data == "admin_skip_photo":
            state = _admin_states.get(user_id)
            if not state or state.get("step") != "WAITING_FOR_CAMPAIGN_PHOTO":
                await event.answer("No active campaign wizard found.", alert=True)
                return
                
            text = state["text"]
            database.add_campaign(text, None)
            _admin_states.pop(user_id, None)
            
            await event.respond("✅ **New text-only campaign added successfully!**")
            await send_admin_panel(event)
            
        # --- List / Delete Campaigns ---
        elif data == "admin_list_campaigns":
            settings = database.get_owner_settings()
            campaigns = settings.get("campaigns", [])
            
            if not campaigns:
                await event.edit(
                    "📢 **No advertisement campaigns configured.**",
                    buttons=[
                        [Button.inline("➕ Add Campaign", "admin_add_campaign")],
                        [Button.inline("🔙 Back", "admin_panel")]
                    ]
                )
                return
                
            text = "📋 **Configured advertisement campaigns:**\n\n"
            buttons = []
            for idx, c in enumerate(campaigns, 1):
                c_id = c.get("id")
                photo_status = "🖼️ Yes" if c.get("photo") else "📝 No"
                # Truncate text for neat listing
                short_text = c.get("text", "")[:40] + "..." if len(c.get("text", "")) > 40 else c.get("text", "")
                text += f"**{idx}.** Photo: `{photo_status}` | ID: `{c_id}`\n   _{short_text}_\n\n"
                buttons.append([Button.inline(f"🗑️ Delete Campaign {idx}", f"admin_del_camp_{c_id}")])
                
            buttons.append([Button.inline("➕ Add Campaign", "admin_add_campaign")])
            buttons.append([Button.inline("🔙 Back", "admin_panel")])
            await event.edit(text, buttons=buttons)
            
        elif data.startswith("admin_del_camp_"):
            c_id = data.replace("admin_del_camp_", "")
            
            # Find photo path if any to delete from disk
            settings = database.get_owner_settings()
            campaigns = settings.get("campaigns", [])
            for c in campaigns:
                if c.get("id") == c_id:
                    photo_path = c.get("photo")
                    if photo_path and os.path.exists(photo_path):
                        try:
                            os.remove(photo_path)
                        except Exception:
                            pass
                            
            success = database.delete_campaign(c_id)
            if success:
                await event.answer("Campaign deleted successfully!", alert=True)
            else:
                await event.answer("Could not find campaign.", alert=True)
                
            # Redisplay list
            settings = database.get_owner_settings()
            campaigns = settings.get("campaigns", [])
            if campaigns:
                # Mock callback event
                class MockEvent:
                    def __init__(self, e):
                        self.sender_id = e.sender_id
                        self.edit = e.edit
                        self.respond = e.respond
                        self.data = b"admin_list_campaigns"
                        self.answer = e.answer
                await admin_callback_handler(MockEvent(event))
            else:
                await send_admin_panel(event, edit=True)
                
        # --- Edit Interval ---
        elif data == "admin_set_interval":
            _admin_states[user_id] = "WAITING_FOR_INTERVAL"
            await event.edit(
                "⏱️ **Please send the broadcast interval in seconds.**\n"
                "Minimum interval: 10 seconds. Recommended: 300+ seconds.",
                buttons=[Button.inline("🔙 Cancel", "admin_panel")]
            )
            
        # --- Toggle Global Spam ---
        elif data == "admin_toggle":
            settings = database.get_owner_settings()
            new_active = not settings.get("is_active", True)
            database.save_owner_settings(
                settings.get("interval", 300),
                new_active
            )
            await event.answer(f"Broadcasting {'Enabled' if new_active else 'Paused'}!", alert=True)
            await send_admin_panel(event, edit=True)
            
        # --- Branding Settings Menu ---
        elif data == "admin_branding":
            settings = database.get_owner_settings()
            name_suffix = settings.get("branding_name_text") or "Not configured (Disabled)"
            bio_suffix = settings.get("branding_bio_text") or "Not configured (Disabled)"
            
            text = (
                "🎨 **Profile Branding Settings**\n\n"
                "Connected userbots will have their Telegram Display Name and Bio "
                "automatically appended with these suffix templates on login.\n\n"
                f"🏷️ **Name Suffix:** `{name_suffix}`\n"
                f"📝 **Bio Suffix:** `{bio_suffix}`"
            )
            buttons = [
                [Button.inline("✏️ Set Name Suffix", "admin_brand_set_name")],
                [Button.inline("✏️ Set Bio Suffix", "admin_brand_set_bio")],
                [Button.inline("🗑️ Clear Branding", "admin_brand_clear")],
                [Button.inline("🔙 Back", "admin_panel")]
            ]
            await event.edit(text, buttons=buttons)
            
        elif data == "admin_brand_set_name":
            _admin_states[user_id] = "WAITING_FOR_BRAND_NAME"
            await event.edit(
                "🏷️ **Send the branding suffix text to append to names.**\n\n"
                "Example: `via @MyBot`",
                buttons=[Button.inline("🔙 Cancel", "admin_branding")]
            )
            
        elif data == "admin_brand_set_bio":
            _admin_states[user_id] = "WAITING_FOR_BRAND_BIO"
            await event.edit(
                "📝 **Send the branding suffix text to append to bios.**\n\n"
                "Example: `via @MyBot`",
                buttons=[Button.inline("🔙 Cancel", "admin_branding")]
            )
            
        elif data == "admin_brand_clear":
            database.save_branding_settings(None, None)
            await event.answer("Branding templates cleared!", alert=True)
            # Re-render menu
            class MockEvent:
                def __init__(self, e):
                    self.sender_id = e.sender_id
                    self.edit = e.edit
                    self.respond = e.respond
                    self.data = b"admin_branding"
                    self.answer = e.answer
            await admin_callback_handler(MockEvent(event))
            
        # --- Start Welcome Settings Menu ---
        elif data == "admin_start_settings":
            settings = database.get_owner_settings()
            start_emoji = settings.get("start_emoji") or "(Empty)"
            start_text = settings.get("start_text") or "(Empty)"
            start_button_text = settings.get("start_button_text", "START ✅")
            
            text = (
                "👋 **Start Welcome Settings**\n\n"
                "When a regular user triggers `/start`, the bot sends a single combined message "
                "along with the WebApp keyboard button.\n\n"
                f"🎈 **Current Start Emoji:** {start_emoji}\n"
                f"📝 **Current Start Text:**\n{start_text}\n\n"
                f"🔘 **Current Button Text:** {start_button_text}"
            )
            buttons = [
                [Button.inline("🎈 Set Start Emoji", "admin_start_set_emoji")],
                [Button.inline("📝 Set Start Text", "admin_start_set_text")],
                [Button.inline("🔘 Set Button Text", "admin_start_set_btn")],
                [Button.inline("🔙 Back", "admin_panel")]
            ]
            await event.edit(text, buttons=buttons, parse_mode='html')
            
        elif data == "admin_start_set_emoji":
            _admin_states[user_id] = "WAITING_FOR_START_EMOJI"
            await event.edit(
                "🎈 **Send the new emoji to be sent on /start.**\n\n"
                "Example: `👎` or `👍` or `🔥`",
                buttons=[Button.inline("🔙 Cancel", "admin_start_settings")]
            )
            
        elif data == "admin_start_set_text":
            _admin_states[user_id] = "WAITING_FOR_START_TEXT"
            await event.edit(
                "📝 **Send the new welcome text to be sent on /start.**\n\n"
                "HTML formatting is supported (e.g. `<b>`, `<i>`, `<a href=\"...\">`).",
                buttons=[Button.inline("🔙 Cancel", "admin_start_settings")]
            )
            
        elif data == "admin_start_set_btn":
            _admin_states[user_id] = "WAITING_FOR_START_BTN"
            await event.edit(
                "🔘 **Send the new text for the WebApp Keyboard Button.**\n\n"
                "Example: `START ✅` or `Open App 🚀`",
                buttons=[Button.inline("🔙 Cancel", "admin_start_settings")]
            )
            
        # --- Userbots List ---
        elif data == "admin_list_bots":
            bots = database.get_all_userbots()
            if not bots:
                await event.edit(
                    "📱 **No userbots connected yet.**",
                    buttons=[Button.inline("🔙 Back", "admin_panel")]
                )
                return
                
            text = "📱 **Connected Userbots & Stats**\n\n"
            for idx, b in enumerate(bots, 1):
                stats = b.get("stats", {})
                bc_count = stats.get("broadcast_count", 0)
                status_emoji = "🟢" if b.get("status") == "active" else "🔴"
                text += (
                    f"{idx}. {status_emoji} **{b.get('name')}** (@{b.get('username') or 'None'})\n"
                    f"   Phone: `{b.get('phone')}`\n"
                    f"   Status: `{b.get('status')}`\n"
                    f"   Sent Ads: `{bc_count}`\n\n"
                )
            
            buttons = [
                [Button.inline("🗑️ Remove Userbot", "admin_remove_prompt")],
                [Button.inline("🔙 Back", "admin_panel")]
            ]
            await event.edit(text, buttons=buttons)
            
        elif data == "admin_remove_prompt":
            _admin_states[user_id] = "WAITING_FOR_REMOVE_PHONE"
            await event.edit(
                "🗑️ **Send the phone number of the userbot to remove (including country code).**\n\n"
                "Example: `+919999999999`",
                buttons=[Button.inline("🔙 Cancel", "admin_panel")]
            )

    # Handle Input from Owner for States or incoming Shared Contacts
    @client.on(events.NewMessage)
    async def message_input_handler(event):
        if not event.is_private:
            return
            
        user_id = event.sender_id
        
        # --- Handle shared contacts for logging in ---
        if event.message.contact:
            contact = event.message.contact
            if contact.user_id != user_id:
                await event.reply("⚠️ Please share your *own* contact, not someone else's.")
                return
                
            phone = "+" + contact.phone_number.strip("+")
            await event.reply("📞 **Contact received!** Requesting login OTP code... Please check the Mini App window.")
            try:
                await event.message.delete()
            except Exception as de:
                logger.warning(f"could not delete contact msg: {de}")
            
            # Start Telethon login request in the background
            asyncio.create_task(web_server.start_login_flow(user_id, phone))
            return

        # --- Handle Admin configuration inputs ---
        if user_id == config.OWNER_ID and user_id in _admin_states:
            state = _admin_states[user_id]
            
            # Simple state is a string
            if isinstance(state, str):
                _admin_states.pop(user_id) # pop state first
                
                if state == "WAITING_FOR_INTERVAL":
                    settings = database.get_owner_settings()
                    try:
                        val = int(event.text.strip())
                        if val < 10:
                            await event.reply("⚠️ Interval must be at least 10 seconds.")
                            _admin_states[user_id] = "WAITING_FOR_INTERVAL"
                            return
                            
                        database.save_owner_settings(
                            val,
                            settings.get("is_active", True)
                        )
                        await event.reply(f"✅ **Interval updated to {val} seconds!**")
                        await send_admin_panel(event)
                    except ValueError:
                        await event.reply("⚠️ Please enter a valid integer.")
                        _admin_states[user_id] = "WAITING_FOR_INTERVAL"
                        
                elif state == "WAITING_FOR_BRAND_NAME":
                    settings = database.get_owner_settings()
                    val = event.text.strip()
                    database.save_branding_settings(val, settings.get("branding_bio_text"))
                    await event.reply(f"✅ **Display Name branding suffix set to:** `{val}`")
                    # Send mock event to show branding menu again
                    class MockEvent:
                        def __init__(self):
                            self.sender_id = user_id
                            self.edit = event.reply
                            self.respond = event.reply
                            self.data = b"admin_branding"
                            self.answer = lambda *args, **kwargs: None
                    await admin_callback_handler(MockEvent())
                    
                elif state == "WAITING_FOR_BRAND_BIO":
                    settings = database.get_owner_settings()
                    val = event.text.strip()
                    database.save_branding_settings(settings.get("branding_name_text"), val)
                    await event.reply(f"✅ **Bio branding suffix set to:** `{val}`")
                    class MockEvent:
                        def __init__(self):
                            self.sender_id = user_id
                            self.edit = event.reply
                            self.respond = event.reply
                            self.data = b"admin_branding"
                            self.answer = lambda *args, **kwargs: None
                    await admin_callback_handler(MockEvent())
                    
                elif state == "WAITING_FOR_START_EMOJI":
                    settings = database.get_owner_settings()
                    val = event.text.strip()
                    database.save_start_settings(val, settings.get("start_text"))
                    await event.reply(f"✅ **Start emoji updated to:** {val}")
                    class MockEvent:
                        def __init__(self):
                            self.sender_id = user_id
                            self.edit = event.reply
                            self.respond = event.reply
                            self.data = b"admin_start_settings"
                            self.answer = lambda *args, **kwargs: None
                    await admin_callback_handler(MockEvent())
                    
                elif state == "WAITING_FOR_START_TEXT":
                    settings = database.get_owner_settings()
                    val = event.text.strip()
                    database.save_start_settings(settings.get("start_emoji"), val)
                    await event.reply(f"✅ **Start welcome text updated!**")
                    class MockEvent:
                        def __init__(self):
                            self.sender_id = user_id
                            self.edit = event.reply
                            self.respond = event.reply
                            self.data = b"admin_start_settings"
                            self.answer = lambda *args, **kwargs: None
                    await admin_callback_handler(MockEvent())

                elif state == "WAITING_FOR_START_BTN":
                    settings = database.get_owner_settings()
                    val = event.text.strip()
                    database.save_start_settings(
                        settings.get("start_emoji"),
                        settings.get("start_text"),
                        val
                    )
                    await event.reply(f"✅ **Start button text updated to:** `{val}`")
                    class MockEvent:
                        def __init__(self):
                            self.sender_id = user_id
                            self.edit = event.reply
                            self.respond = event.reply
                            self.data = b"admin_start_settings"
                            self.answer = lambda *args, **kwargs: None
                    await admin_callback_handler(MockEvent())

                elif state == "WAITING_FOR_REMOVE_PHONE":
                    phone = event.text.strip().replace(" ", "")
                    if not phone.startswith("+") or not phone[1:].isdigit():
                        await event.reply("⚠️ Invalid phone layout. Must start with + followed by digits.")
                        _admin_states[user_id] = "WAITING_FOR_REMOVE_PHONE"
                        return
                        
                    bot_rec = database.get_userbot(phone)
                    if not bot_rec:
                        await event.reply("❌ No connected userbot found for this phone number.")
                        await send_admin_panel(event)
                        return
                        
                    await manager.stop_userbot(phone)
                    database.delete_userbot(phone)
                    
                    # Delete local session file
                    session_path = os.path.join(config.SESSION_DIR, f"{phone}.session")
                    import glob
                    for f in glob.glob(session_path + "*"):
                        try:
                            os.remove(f)
                        except Exception:
                            pass
                            
                    await event.reply(f"✅ **Userbot {phone} stopped, deleted, and session cleared!**")
                    await send_admin_panel(event)

            # Complex state is a wizard flow dictionary
            elif isinstance(state, dict):
                step = state["step"]
                
                if step == "WAITING_FOR_CAMPAIGN_TEXT":
                    # Storing ad text
                    state["text"] = event.text.strip()
                    state["step"] = "WAITING_FOR_CAMPAIGN_PHOTO"
                    
                    buttons = [[Button.inline("Skip Photo (Text-Only)", "admin_skip_photo")]]
                    await event.reply(
                        "🖼️ **Please send/upload the photo for this campaign**, or click the button below to skip photo upload.",
                        buttons=buttons
                    )
                    
                elif step == "WAITING_FOR_CAMPAIGN_PHOTO":
                    if event.text and event.text.strip().startswith("/skip"):
                        # Skip trigger via command fallback
                        text = state["text"]
                        database.add_campaign(text, None)
                        _admin_states.pop(user_id, None)
                        await event.reply("✅ **New text-only campaign added successfully!**")
                        await send_admin_panel(event)
                        return
                        
                    if not event.message.photo:
                        await event.reply("⚠️ That was not a photo. Please send a photo or click/type /skip.")
                        return
                        
                    # Owner sent a photo
                    _admin_states.pop(user_id, None) # clean state
                    text = state["text"]
                    
                    # Generate a unique path for the photo
                    photo_filename = f"ad_photo_{uuid.uuid4().hex[:8]}.jpg"
                    photo_path = os.path.join(config.DOWNLOADS_DIR, photo_filename)
                    
                    await event.reply("📥 Downloading campaign image...")
                    await event.client.download_media(event.message.photo, file=photo_path)
                    
                    database.add_campaign(text, photo_path)
                    await event.reply("✅ **New campaign added successfully (with image)!**")
                    await send_admin_panel(event)

async def send_admin_panel(event, edit=False):
    settings = database.get_owner_settings()
    active_status = "🟢 ACTIVE" if settings.get("is_active") else "🔴 PAUSED"
    campaigns = settings.get("campaigns", [])
    
    text = (
        "👑 **Owner Control Panel**\n\n"
        f"📢 **Configured Campaigns:** `{len(campaigns)}`\n"
        f"⏱️ **Broadcast Interval:** `{settings.get('interval')} seconds`\n"
        f"⚡ **Broadcasting Status:** `{active_status}`\n\n"
        "Configure rotating messages, name/bio branding, or manage connected accounts using the buttons below."
    )
    
    toggle_text = "⏸️ Pause Broadcasting" if settings.get("is_active") else "▶️ Resume Broadcasting"
    
    buttons = [
        [Button.inline("📋 Manage Ad Campaigns", "admin_list_campaigns")],
        [Button.inline("👋 Welcome Start Settings", "admin_start_settings")],
        [Button.inline("🎨 Branding Settings", "admin_branding")],
        [Button.inline("⏱️ Edit Interval", "admin_set_interval")],
        [Button.inline(toggle_text, "admin_toggle")],
        [Button.inline("📱 View Connected Userbots", "admin_list_bots")]
    ]
    
    if edit:
        await event.edit(text, buttons=buttons)
    else:
        await event.respond(text, buttons=buttons)
