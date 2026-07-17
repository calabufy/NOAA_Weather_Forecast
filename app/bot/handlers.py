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

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import BotCommand, Message

from app import config, metrics, timeutil
from app.bot import formatting
from app.db import repo

log = logging.getLogger(__name__)
router = Router()

# Подсказки команд в меню Telegram (кнопка «/» слева от поля ввода).
# Порядок = порядок отображения; регистрируются на старте в main.py.
BOT_COMMANDS = [
    BotCommand(command="forecast", description="Прогноз Tmax на завтра (NBM, MAV, MET)"),
    BotCommand(command="errors", description="Метрики ошибок по окнам"),
    BotCommand(command="help", description="Справка: модели, метрики, циклы"),
    BotCommand(command="start", description="Краткая справка"),
]


async def setup_commands(bot: Bot) -> None:
    """Зарегистрировать список команд в меню бота (идемпотентно)."""
    await bot.set_my_commands(BOT_COMMANDS)

_START = (
    "Прогноз Tmax по станции KLAX (Лос-Анджелес) и статистика ошибок моделей.\n\n"
    "<b>/forecast</b> — прогноз максимума на завтра (NBM, MAV, MET).\n"
    "<b>/errors</b> — метрики качества по окнам 7д/30д/сезон/год.\n"
    "<b>/help</b> — подробная справка: модели, метрики, расписание циклов."
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(_START)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(formatting.format_help())


@router.message(Command("forecast"))
async def cmd_forecast(message: Message) -> None:
    # Прогноз читается из БД (её каждые ~6 часов пополняет fetch-джоб), а не
    # живым забором бюллетеней (app/bot/live.py, исторический режим VPS):
    # в webhook-режиме Telegram ждёт ответа не дольше 60 секунд, скачивание
    # NBS (~28 МБ) в лимит не укладывается и блокирует доставку всех апдейтов.
    # Цена — прогноз может отставать от свежайшего цикла максимум на ~6 часов.
    target = timeutil.la_tomorrow()
    try:
        conn = repo.connect()
        try:
            points = {
                model: repo.latest_forecast(conn, target, model)
                for model in config.BOT_MODELS
            }
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — пользователь должен получить ответ при сбое
        log.exception("сбой /forecast")
        await message.answer("Не удалось получить прогноз, попробуйте позже.")
        return
    if not any(points.values()):
        await message.answer("Прогноз на завтра ещё не собран, попробуйте позже.")
        return
    await message.answer(formatting.format_forecast(target, points))


@router.message(Command("errors"))
async def cmd_errors(message: Message) -> None:
    ref = timeutil.la_today()
    # Самое дальнее начало среди всех окон — тянем ряд ошибок один раз на модель
    # за этот диапазон, а metrics.report сам режет его по окнам.
    bounds = metrics.window_bounds(ref)
    earliest = min(start for start, _ in bounds.values())
    end = max(end for _, end in bounds.values())  # вчера (за сегодня факта нет)

    # Единая таблица model_daily_errors за всё время: архивный бэкфилл +
    # оперативные дни от verify-джоба. Окна считает metrics.report на лету.
    conn = repo.connect()
    try:
        reports = {
            model: metrics.report(
                repo.daily_error_series(conn, model, earliest, end), ref
            )
            for model in config.BOT_MODELS
        }
    finally:
        conn.close()
    await message.answer(formatting.format_errors(reports))
