import asyncio
import logging
import signal
import time

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeChat

from admin.app import app as admin_app
from bot.handlers import router
from core.config import settings
from core.db import get_db, init_db
from core.llm import llm_client
from core.moods import mood_ttl_task, trim_switch_log
from core.profiles import profiles_task

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL_SECONDS = 3600
VACUUM_EVERY_N_CLEANUPS = 24
# Must stay below the container's stop_grace_period, or Docker escalates to SIGKILL.
SHUTDOWN_TIMEOUT_SECONDS = 10

# Scoped to the group only — this bot serves a single group, not private chats.
# /profile itself stays admin-only in the handler (bot/handlers/profile.py); listing
# it here is just a menu entry, not a permission grant.
COMMANDS = [
    BotCommand(command="summary", description="Пересказ последних сообщений (можно указать часы)"),
    BotCommand(command="mood", description="Текущее настроение бота / переключить"),
    BotCommand(command="profile", description="Заметка об участнике — реплаем на его сообщение (админы)"),
    BotCommand(command="forgetme", description="Стереть свою заметку, отказаться от профилирования"),
]


async def _register_commands(bot: Bot) -> None:
    """Overwrites the command list for this chat — including leftovers from a previous
    bot framework that BotFather's own UI can't reach, since commands live in separate
    slots per (scope, language_code) and BotFather only edits the unscoped default.

    Covers the common language_code variants too: a scoped-but-language-specific
    leftover would otherwise still win over our language-less entry for those clients.
    """
    scope = BotCommandScopeChat(chat_id=settings.GROUP_CHAT_ID)
    try:
        for language_code in (None, "ru", "en"):
            await bot.set_my_commands(COMMANDS, scope=scope, language_code=language_code)
    except Exception:
        logger.exception("Failed to register bot commands, continuing without it")


async def cleanup_task() -> None:
    cleanups_since_vacuum = 0
    while True:
        cutoff = int(time.time()) - settings.HISTORY_TTL_HOURS * 3600
        async with get_db() as db:
            cursor = await db.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
            await db.commit()
            logger.info("Cleanup: deleted %d messages older than %dh", cursor.rowcount, settings.HISTORY_TTL_HOURS)

            cleanups_since_vacuum += 1
            if cleanups_since_vacuum >= VACUUM_EVERY_N_CLEANUPS:
                await db.execute("VACUUM")
                cleanups_since_vacuum = 0
                logger.info("Cleanup: VACUUM complete")

        await trim_switch_log()

        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM's default action kills the process outright, skipping every cleanup
    path, so it has to be handled explicitly for `docker stop` to be graceful."""
    loop = asyncio.get_running_loop()

    def request_stop(sig: signal.Signals) -> None:
        # uvicorn also captures signals and re-delivers them to the previous
        # handler, so this can fire more than once for a single SIGTERM.
        if stop.is_set():
            return
        logger.info("Received %s, shutting down", sig.name)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop, sig)
        except NotImplementedError:  # not supported on this platform
            logger.warning("Cannot install a handler for %s here", sig.name)


async def main() -> None:
    logging.basicConfig(
        level=settings.LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    await init_db()

    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    await _register_commands(bot)

    server = uvicorn.Server(
        uvicorn.Config(
            admin_app,
            host="0.0.0.0",
            port=settings.ADMIN_PORT,
            log_level=settings.LOG_LEVEL.lower(),
        )
    )

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    await llm_client.start()

    serving = [
        asyncio.create_task(dp.start_polling(bot), name="bot"),
        asyncio.create_task(server.serve(), name="admin"),
    ]
    background = [
        asyncio.create_task(cleanup_task(), name="cleanup"),
        asyncio.create_task(profiles_task(bot.id), name="profiles"),
        asyncio.create_task(mood_ttl_task(settings.GROUP_CHAT_ID), name="moods"),
    ]
    waiter = asyncio.create_task(stop.wait(), name="signal")

    try:
        # Exit on a signal, or as soon as any component dies.
        done, _ = await asyncio.wait(
            [*serving, *background, waiter], return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if task is not waiter and not task.cancelled() and task.exception() is not None:
                raise task.exception()
    finally:
        await _shutdown(bot, dp, server, serving, background, waiter)


async def _shutdown(bot, dp, server, serving, background, waiter) -> None:
    logger.info("Shutdown: stopping components")
    waiter.cancel()

    # Timers hold nothing that needs flushing; stop them at once.
    for task in background:
        task.cancel()

    # Ask the pollers/servers to finish their current work first.
    server.should_exit = True
    try:
        await dp.stop_polling()
    except Exception:
        pass  # polling may never have started (e.g. a bad token)

    _, pending = await asyncio.wait(serving, timeout=SHUTDOWN_TIMEOUT_SECONDS)
    for task in pending:
        logger.warning("Shutdown: forcing %s to stop", task.get_name())
        task.cancel()

    await asyncio.gather(*serving, *background, waiter, return_exceptions=True)

    # aiosqlite connections are per-operation and closed by their context managers,
    # so cancelling above cannot leave one open; a write cut off before its commit
    # simply rolls back. Only the long-lived HTTP clients need closing here.
    await llm_client.close()
    await bot.session.close()
    logger.info("Shutdown: complete")


if __name__ == "__main__":
    asyncio.run(main())
