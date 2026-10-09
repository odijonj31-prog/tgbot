import asyncio
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent

from . import config, db, gate
from .gate import Gate
from .handlers import admin, user

log = logging.getLogger("bot")

COMMANDS = {
    "uz": [("start", "Boshlash"), ("settings", "Sozlamalar"), ("history", "Tarix"), ("lang", "Til")],
    "ru": [("start", "Начать"), ("settings", "Настройки"), ("history", "История"), ("lang", "Язык")],
    "en": [("start", "Start"), ("settings", "Settings"), ("history", "History"), ("lang", "Language")],
}


async def cleanup_loop():
    while True:
        await asyncio.sleep(600)
        try:
            now = time.time()
            if config.TMP_DIR.exists():
                for p in config.TMP_DIR.iterdir():
                    if p.is_file() and now - p.stat().st_mtime > config.FILE_TTL:
                        p.unlink(missing_ok=True)
            for k, p in list(user.CACHE.items()):
                if not p.exists():
                    user.CACHE.pop(k, None)
            while len(user.QUERIES) > 500:
                user.QUERIES.pop(next(iter(user.QUERIES)))
            gate.prune()
        except Exception:
            log.exception("Tozalashda xato")


async def on_error(event: ErrorEvent) -> bool:
    log.error("Handler xatosi: %s", event.exception, exc_info=event.exception)
    return True


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN o'rnatilmagan")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    await db.init(config.DB_PATH)

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(Gate())
    dp.callback_query.outer_middleware(Gate())
    dp.include_router(admin.router)   # admin birinchi
    dp.include_router(user.router)
    dp.errors.register(on_error)

    for code, cmds in COMMANDS.items():
        try:
            await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in cmds],
                                      language_code=code if code != "uz" else "uz")
        except Exception:
            log.warning("Komandalarni o'rnatib bo'lmadi (%s)", code)
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS["uz"]])
    except Exception:
        pass

    me = await bot.me()
    log.info("Bot ishga tushdi: @%s", me.username)
    for aid in config.ADMIN_IDS:
        try:
            await bot.send_message(aid, f"✅ @{me.username} ishga tushdi. /admin")
        except Exception:
            pass

    task = asyncio.create_task(cleanup_loop())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
