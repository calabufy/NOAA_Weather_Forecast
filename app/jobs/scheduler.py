# scheduler.py — настройка APScheduler.
# Регистрирует джобы fetch_forecasts и verify по cron-расписанию (в таймзоне LA)
# и запускает их в том же asyncio-loop, что и бот (Фаза 4).
#
# Джобы синхронные (blocking httpx), поэтому в async-loop их выполняем через
# asyncio.to_thread, не блокируя цикл событий. Соединение с SQLite открывается
# ВНУТРИ рабочего потока (своё на каждый запуск) и там же закрывается — так оно
# не пересекает границу потоков (sqlite3 check_same_thread) и не конфликтует.
# Ретраи внешних запросов обеспечивает tenacity в HTTP-слое источников; здесь —
# несколько запусков в течение суток (см. config.FETCH_HOURS_LA/VERIFY_HOURS_LA),
# что и даёт «повторные попытки», а идемпотентность записи не создаёт дублей.

from __future__ import annotations

import asyncio
import logging

from app import config
from app.db import repo
from app.jobs import fetch_forecasts, verify

log = logging.getLogger(__name__)


def _fetch_sync(db_path: str) -> None:
    conn = repo.connect(db_path)
    try:
        fetch_forecasts.run(conn)
    finally:
        conn.close()


def _verify_sync(db_path: str) -> None:
    conn = repo.connect(db_path)
    try:
        verify.run(conn)
    finally:
        conn.close()


async def _run_fetch(db_path: str) -> None:
    # Соединение открываем ВНУТРИ рабочего потока (to_thread), а не в loop-потоке:
    # sqlite3 по умолчанию check_same_thread=True, и переиспользование соединения
    # из чужого потока даёт ProgrammingError. Каждая сессия — свой connect/close.
    await asyncio.to_thread(_fetch_sync, db_path)


async def _run_verify(db_path: str) -> None:
    await asyncio.to_thread(_verify_sync, db_path)


def build_scheduler(db_path: str | None = None):
    """Создать AsyncIOScheduler с зарегистрированными джобами (не запущен).

    Вызывающий (main.py на Фазе 4) делает scheduler.start() внутри работающего
    asyncio-loop. apscheduler импортируется лениво — модуль остаётся импортируемым
    и без установленной зависимости (нужна только при реальном запуске сервиса).
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    db_path = db_path or config.DB_PATH
    scheduler = AsyncIOScheduler(timezone=config.TZ)

    scheduler.add_job(
        _run_fetch, CronTrigger(hour=",".join(map(str, config.FETCH_HOURS_LA)),
                                minute=30, timezone=config.TZ),
        args=[db_path], id="fetch_forecasts", max_instances=1,
        coalesce=True, misfire_grace_time=3600,
    )
    scheduler.add_job(
        _run_verify, CronTrigger(hour=",".join(map(str, config.VERIFY_HOURS_LA)),
                                 minute=0, timezone=config.TZ),
        args=[db_path], id="verify", max_instances=1,
        coalesce=True, misfire_grace_time=3600,
    )
    log.info("джобы запланированы: fetch@%s:30, verify@%s:00 (%s)",
             config.FETCH_HOURS_LA, config.VERIFY_HOURS_LA, config.TZ)
    return scheduler
