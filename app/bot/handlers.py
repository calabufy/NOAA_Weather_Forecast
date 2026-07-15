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
from contextlib import suppress

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import BotCommand, LinkPreviewOptions, Message

from app import config, metrics, timeutil
from app.bot import formatting, live
from app.db import repo

log = logging.getLogger(__name__)
router = Router()

# Подсказки команд в меню Telegram (кнопка «/» слева от поля ввода).
# Порядок = порядок отображения; регистрируются на старте в main.py.
BOT_COMMANDS = [
    BotCommand(command="forecast",
               description="Прогноз Tmax на завтра (NBM, MAV, MET) + Polymarket"),
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
    # Ленивый забор свежего цикла в момент запроса (не из БД): пользователю нужен
    # актуальнейший прогноз, а не «зачётный». Кэш в live гасит повторные заборы.
    # Забор может занять секунды (качаем бюллетень) — показываем плашку загрузки,
    # затем удаляем её и шлём готовый прогноз. Рядом с прогнозом — рынок
    # Polymarket на те же сутки (live.market_for сам гасит свои сбои -> None).
    loading = await message.answer("Загрузка прогноза...")
    try:
        target, points = await live.forecast_tomorrow()
        market = await live.market_for(target)
    except Exception:  # noqa: BLE001 — пользователь должен получить ответ при сбое
        log.exception("сбой /forecast")
        await message.answer("Не удалось получить прогноз, попробуйте позже.")
        return
    finally:
        with suppress(Exception):
            await loading.delete()
    await message.answer(
        formatting.format_forecast(target, points)
        + "\n\n"
        + formatting.format_market(market, points),
        # Без превью: ссылка на Polymarket разворачивалась бы в громоздкую карточку.
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


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
        actuals = repo.list_actuals(conn, earliest, end)
        reports = {
            model: metrics.report(
                repo.error_series(
                    conn, model, earliest, end, actuals=actuals
                ),
                ref,
            )
            for model in config.BOT_MODELS
        }
    finally:
        conn.close()
    await message.answer(formatting.format_errors(reports))
