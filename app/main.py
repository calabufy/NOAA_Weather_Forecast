# main.py — точка входа сервиса.
# Инициализирует БД, поднимает планировщик APScheduler с джобами (fetch/verify)
# и запускает Telegram-бота (long polling) в одном общем asyncio-loop.
#
# Два режима работы, оба через одну и ту же run():
#   - бесконечный (локально/VPS): планировщик джобов внутри процесса, polling
#     до сигнала завершения — исторический режим для постоянно работающего сервиса.
#   - ограниченный по времени (BOT_POLL_SECONDS, GitHub Actions): процесс живёт
#     считанные минуты по cron, планировщик не нужен (fetch/verify — отдельные
#     воркфлоу со своим cron, см. scripts/run_job.py), polling останавливается
#     сам по истечении таймаута.

from __future__ import annotations

import asyncio
import logging
import os

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


async def run(max_seconds: float | None = None) -> None:
    """Собрать и запустить сервис в текущем asyncio-loop.

    max_seconds=None -> бесконечный polling + внутренний планировщик (как раньше).
    max_seconds=N -> завершиться самому через N секунд (режим коротких сессий
    в GitHub Actions); планировщик не поднимается — fetch/verify гоняет отдельный
    cron-воркфлоу через scripts/run_job.py.
    """
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

    scheduler = None
    if max_seconds is None:
        scheduler = build_scheduler()
        scheduler.start()
    log.info("сервис запущен: бот (long polling)%s",
             "" if max_seconds is None else f", ограничение {max_seconds:.0f}с")
    try:
        # НЕ дропаем накопившиеся апдейты: в режиме коротких сессий (раз в
        # несколько минут по cron) это выбросило бы сообщения, пришедшие между
        # запусками. delete_webhook нужен только чтобы снять webhook, если он
        # когда-то был установлен — polling и webhook несовместимы у Telegram.
        await bot.delete_webhook(drop_pending_updates=False)
        await handlers.setup_commands(bot)
        if max_seconds is None:
            await dp.start_polling(bot)
        else:
            # close_bot_session=False: сессию закрываем сами в finally, независимо
            # от того, остановился ли polling по таймауту или упал раньше него.
            polling_task = asyncio.create_task(
                dp.start_polling(bot, close_bot_session=False)
            )
            _, pending = await asyncio.wait({polling_task}, timeout=max_seconds)
            if polling_task in pending:
                await dp.stop_polling()
                await polling_task
            else:
                polling_task.result()  # polling завершился раньше таймаута — пробросить ошибку
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        await bot.session.close()


def main() -> None:
    _configure_logging()
    raw = os.getenv("BOT_POLL_SECONDS")
    asyncio.run(run(float(raw) if raw else None))


if __name__ == "__main__":
    main()
