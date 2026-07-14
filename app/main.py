# main.py — точка входа сервиса.
# Инициализирует БД, поднимает планировщик APScheduler с джобами (fetch/verify)
# и запускает Telegram-бота (long polling) в одном общем asyncio-loop.
# Всё приложение — один процесс.

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app import config
from app.bot import handlers, middleware
from app.db import repo
from app.jobs.scheduler import build_scheduler

log = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def run() -> None:
    """Собрать и запустить сервис в текущем asyncio-loop."""
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан — заполните .env (см. .env.example)")
    if not config.ALLOWED_CHAT_IDS:
        log.warning("ALLOWED_CHAT_IDS пуст — бот не ответит никому")

    # БД: создаём/мигрируем один раз на старте (джобы и хендлеры открывают свои
    # соединения по мере надобности).
    repo.connect().close()

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(handlers.router)
    dp.message.middleware(middleware.AllowlistMiddleware(config.ALLOWED_CHAT_IDS))

    # Алерты пайплайна в чат: handler на корневом логгере ловит ERROR+ из джобов
    # и источников и шлёт в allowlist через loop бота.
    loop = asyncio.get_running_loop()
    middleware.install_alert_handler(bot, loop, config.ALLOWED_CHAT_IDS)

    scheduler = build_scheduler()
    scheduler.start()
    log.info("сервис запущен: бот (long polling) + планировщик джобов")
    try:
        # Снимаем webhook (если был) и отбрасываем накопившиеся апдейты —
        # иначе зарегистрированный webhook конфликтует с long polling.
        await bot.delete_webhook(drop_pending_updates=True)
        await handlers.setup_commands(bot)
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


def main() -> None:
    _configure_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
