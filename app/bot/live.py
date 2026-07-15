# live.py — ленивый забор прогноза на завтра для команды /forecast.
# В отличие от коллектора (fetch_forecasts пишет снимки в БД под верификацию),
# здесь прогноз тянется прямо в момент запроса и в БД НЕ пишется: пользователю
# нужен свежайший цикл, а не «зачётный». Результат кэшируется на короткий TTL,
# чтобы не качать бюллетени (особенно NBS ~28 МБ) на каждый /forecast; ключ кэша
# включает цикл, поэтому выход нового цикла сбрасывает кэш независимо от TTL.
#
# Блокирующие httpx-запросы источников выполняем через asyncio.to_thread, чтобы
# не вешать общий loop бота. Сбой одной модели изолирован (даёт «—» в выдаче) и
# не мешает второй — как и в коллекторе.

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date

from app import config, timeutil
from app.jobs.fetch_forecasts import latest_cycle
from app.sources import ForecastPoint, mav, met, nbm

log = logging.getLogger(__name__)

# Кэш: (run_date, cycle) -> (monotonic-время забора, {model: list[ForecastPoint]}).
# Актуален всегда только текущий цикл, поэтому при записи чистим прочие ключи.
_cache: dict[tuple[date, str], tuple[float, dict[str, list[ForecastPoint]]]] = {}
_lock = asyncio.Lock()


def _fetch_all(run_date: date, cycle: str) -> dict[str, list[ForecastPoint]]:
    """Синхронно забрать NBM и MAV за цикл; сбой каждой модели изолирован.

    Возвращает {model: list[ForecastPoint]}; при сбое модели — пустой список.
    Блокирует поток (httpx) — вызывать только через asyncio.to_thread.
    """
    sources = (
        (nbm.MODEL, lambda: nbm.fetch_forecast(run_date, cycle)),
        (mav.MODEL, lambda: mav.fetch_forecast(cycle)),
        (met.MODEL, lambda: met.fetch_forecast(cycle)),
    )
    out: dict[str, list[ForecastPoint]] = {}
    for model, fetch in sources:
        try:
            out[model] = fetch()
        except Exception:  # noqa: BLE001 — сбой модели не должен ронять /forecast
            log.exception("ленивый сбор прогноза %s не удался", model)
            out[model] = []
    return out


async def _fetch_cached(run_date: date, cycle: str) -> dict[str, list[ForecastPoint]]:
    """Забор с TTL-кэшем по циклу; повторные запросы в пределах TTL — из кэша.

    Кэшируем только непустой результат: при полном сетевом провале следующий
    запрос попробует снова, а не «залипнет» на весь TTL. Забор под _lock, чтобы
    параллельные /forecast не качали бюллетень одновременно.
    """
    key = (run_date, cycle)
    async with _lock:
        now = time.monotonic()
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < config.FORECAST_CACHE_TTL_SEC:
            return hit[1]
        points = await asyncio.to_thread(_fetch_all, run_date, cycle)
        if any(points.values()):  # хоть одна модель дала данные — кэшируем
            _cache.clear()  # актуален только текущий цикл
            _cache[key] = (now, points)
        return points


async def forecast_tomorrow() -> tuple[date, dict[str, ForecastPoint | None]]:
    """Прогноз Tmax на завтра по моделям — ленивый забор свежего цикла.

    Возвращает (target_date, {model: ForecastPoint | None}) в порядке
    config.BOT_MODELS. None — модель не дала прогноз на эти сутки (сбой источника
    или в свежем цикле нет колонки на завтра).
    """
    run_date, cycle = latest_cycle()
    target = timeutil.la_tomorrow()
    by_model = await _fetch_cached(run_date, cycle)
    return target, {
        model: next(
            (p for p in by_model.get(model, ()) if p.target_date == target), None
        )
        for model in config.BOT_MODELS
    }
