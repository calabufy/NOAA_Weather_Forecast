# handlers.py — обработчики команд бота.
# /start (краткий help), /forecast (прогноз Tmax на завтра по моделям),
# /errors (таблица метрик по окнам), /chart (график сравнения моделей).
#
# Данные читаются из SQLite: на каждый запрос открываем своё короткоживущее
# соединение и закрываем его (repo.connect идемпотентен и дёшев для SQLite) —
# так хендлеры не держат общее состояние и не конфликтуют с джобами-писателями,
# работающими в отдельных потоках. Чтение блокирующее, но микроскопическое,
# поэтому выполняем прямо в loop без to_thread.

from __future__ import annotations

import asyncio
import logging
from datetime import date

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import BotCommand, BufferedInputFile, LinkPreviewOptions, Message

from app import config, metrics, timeutil
from app.bot import charts, formatting
from app.db import repo
from app.sources import polymarket

log = logging.getLogger(__name__)
router = Router()

# Подсказки команд в меню Telegram (кнопка «/» слева от поля ввода).
# Порядок = порядок отображения; регистрируются на старте в main.py.
BOT_COMMANDS = [
    BotCommand(command="forecast", description="Прогноз Tmax на завтра (NBM, MAV, MET)"),
    BotCommand(command="errors", description="Метрики ошибок по окнам"),
    BotCommand(command="chart", description="График метрик NBM, MAV и MET"),
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
    "<b>/chart</b> — график сравнения качества моделей.\n"
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

    # Блок рынка Polymarket — best-effort: сбой/отсутствие события не должны
    # лишить пользователя прогноза. Забор блокирующий (httpx) — через to_thread.
    market = None
    try:
        market = await asyncio.to_thread(polymarket.fetch_market, target)
    except Exception:  # noqa: BLE001 — рынок вторичен, прогноз важнее
        log.warning("polymarket недоступен — блок рынка пропущен", exc_info=True)

    text = formatting.format_forecast(target, points)
    if market is not None:
        text += "\n\n" + formatting.format_market(market, points)
    # Превью ссылки отключаем: иначе Telegram приклеит большую карточку рынка.
    await message.answer(
        text, link_preview_options=LinkPreviewOptions(is_disabled=True)
    )


@router.message(Command("errors"))
async def cmd_errors(message: Message) -> None:
    ref = timeutil.la_today()
    reports = _read_metric_reports(ref)
    await message.answer(formatting.format_errors(reports))


def _read_metric_reports(ref: date) -> charts.Reports:
    """Прочитать единый ряд ошибок и посчитать те же агрегаты для текста и PNG."""
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
    return reports


@router.message(Command("chart"))
async def cmd_chart(message: Message) -> None:
    ref = timeutil.la_today()
    try:
        reports = _read_metric_reports(ref)
        if not charts.has_data(reports):
            await message.answer("Для графика пока недостаточно данных.")
            return
        png = charts.render_metrics_chart(reports, ref)
        end = metrics.window_bounds(ref)["year"][1]
        photo = BufferedInputFile(png, filename=f"klax-metrics-{end.isoformat()}.png")
        await message.answer_photo(
            photo,
            caption=(
                f"Метрики моделей KLAX по {end.isoformat()} включительно. "
                "Точные значения и объём выборки: /errors"
            ),
        )
    except Exception:  # noqa: BLE001 — пользователь должен получить ответ при сбое
        log.exception("сбой /chart")
        await message.answer("Не удалось построить график, попробуйте позже.")
