import logging
import asyncio
from typing import Dict
from userbot import UserBotSession
import database

logger = logging.getLogger(__name__)

# Registry of active userbot sessions: phone -> UserBotSession instance
_running_bots: Dict[str, UserBotSession] = {}

async def start_userbot(phone: str) -> bool:
    """
    Instantiates and starts a userbot. If already running, stops it first.
    """
    await stop_userbot(phone)
    
    bot = UserBotSession(phone)
    success = await bot.start()
    if success:
        _running_bots[phone] = bot
        return True
    return False

async def stop_userbot(phone: str):
    """
    Stops a running userbot and removes it from the running registry.
    """
    if phone in _running_bots:
        bot = _running_bots.pop(phone)
        await bot.stop()

async def start_all_userbots():
    """
    Loads all active/error/restricted userbots from the database and starts them.
    Skipping 'unauthorized' sessions.
    """
    logger.info("Resuming all active userbots from database...")
    userbots = database.get_all_userbots()
    
    tasks = []
    for ub in userbots:
        phone = ub.get("phone")
        status = ub.get("status")
        if phone and status != "unauthorized":
            tasks.append(start_userbot(phone))
            
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        started_count = sum(1 for r in results if r is True)
        logger.info(f"Resumed {started_count} of {len(tasks)} userbot sessions.")
    else:
        logger.info("No active userbots to resume.")

async def stop_all_userbots():
    """
    Stops all running userbots gracefully.
    """
    logger.info("Stopping all running userbot sessions...")
    phones = list(_running_bots.keys())
    for phone in phones:
        await stop_userbot(phone)
    logger.info("All userbot sessions stopped.")
