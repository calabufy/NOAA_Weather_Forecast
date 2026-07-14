# handlers.py — обработчики команд бота.
# /start (краткий help), /forecast (прогноз Tmax на завтра по моделям),
# /errors (таблица метрик по окнам). Берёт данные из repo и metrics.
#
# Данные читаются из SQLite: на каждый запрос открываем своё короткоживущее
# соединение и закрываем его (repo.connect идемпотентен и дёшев для SQLite) —
# так хендлеры не держат общее состояние и не конфликтуют с джобами-писателями,
# работающими в отдельных потоках. Чтение блокирующее, но микроскопическое,
# поэтому выполняем прямо в loop без to_thread.

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app import config, metrics, timeutil
from app.bot import formatting
from app.db import repo

log = logging.getLogger(__name__)
router = Router()

_HELP = (
    "Прогноз Tmax по станции KLAX (Лос-Анджелес) и статистика ошибок моделей.\n\n"
    "<b>/forecast</b> — прогноз максимума на завтра (NBM и MAV).\n"
    "<b>/errors</b> — метрики качества по окнам 7д/30д/сезон/год.\n"
    "<b>/start</b> — эта справка."
)


@router.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    await message.answer(_HELP)


@router.message(Command("forecast"))
async def cmd_forecast(message: Message) -> None:
    target = timeutil.la_tomorrow()
    conn = repo.connect()
    try:
        points = {
            model: repo.latest_forecast(conn, target, model)
            for model in config.BOT_MODELS
        }
    finally:
        conn.close()
    await message.answer(formatting.format_forecast(target, points))


@router.message(Command("errors"))
async def cmd_errors(message: Message) -> None:
    ref = timeutil.la_today()
    # Самое дальнее начало среди всех окон — тянем ряд ошибок один раз на модель
    # за этот диапазон, а metrics.report сам режет его по окнам.
    bounds = metrics.window_bounds(ref)
    earliest = min(start for start, _ in bounds.values())
    end = max(end for _, end in bounds.values())  # вчера (за сегодня факта нет)

    conn = repo.connect()
    try:
        reports = {
            model: metrics.report(
                repo.error_series(conn, model, earliest, end), ref
            )
            for model in config.BOT_MODELS
        }
    finally:
        conn.close()
    await message.answer(formatting.format_errors(reports))
