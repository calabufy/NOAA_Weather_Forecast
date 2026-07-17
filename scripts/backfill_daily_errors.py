"""Импорт годовой истории MOS/NBM в единую таблицу дневных ошибок.

Прогнозы: Iowa Environmental Mesonet MOS Archive (исходные продукты NWS).
Факт: NOAA NCEI Daily Summaries, KLAX/GHCN station USW00023174.

Пишет в model_daily_errors — ту же таблицу, что наполняет verify-джоб
оперативными днями; оперативные строки бэкфилл не перезаписывает (правило
в repo.upsert_daily_errors), поэтому его можно безопасно перегонять.
Оперативные таблицы forecasts/actuals не читаются и не изменяются;
агрегаты по окнам считает app/metrics.py на лету при /errors.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlencode

from app import timeutil
from app.db import repo
from app.db.daily_errors import ModelDayError
from app.sources import http_get_json

UTC = timezone.utc
log = logging.getLogger("backfill_daily_errors")

IEM_ENDPOINT = "https://mesonet.agron.iastate.edu/cgi-bin/request/mos.py"
NCEI_ENDPOINT = "https://www.ncei.noaa.gov/access/services/data/v1"
NCEI_STATION = "USW00023174"
FORECAST_SOURCE = "IEM_MOS_ARCHIVE"
ACTUAL_SOURCE = "NOAA_NCEI_DAILY_SUMMARIES"

# Текущие имена проекта -> модель и поле максимума в IEM API.
IEM_MODELS = {
    "NBM": ("NBS", "txn"),
    "MAV": ("GFS", "n_x"),
    "MET": ("NAM", "n_x"),
}


def _parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _month_after(dt: datetime) -> datetime:
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1, day=1)
    return dt.replace(month=dt.month + 1, day=1)


def _iem_url(model: str, start: datetime, end: datetime) -> str:
    params = {
        "station": "KLAX",
        "model": model,
        "sts": start.strftime("%Y-%m-%dT%H:%MZ"),
        "ets": end.strftime("%Y-%m-%dT%H:%MZ"),
        "format": "json",
    }
    return f"{IEM_ENDPOINT}?{urlencode(params)}"


def parse_iem_rows(
    payload: list[dict], model: str, marker: str, start: date, end: date
) -> dict[date, tuple[datetime, float]]:
    """Выбрать последний цикл до local midnight для каждой даты.

    MOS max/min стоит в колонках с чередующимися дневным максимумом и ночным
    минимумом. Как и live-парсер, принимаем только колонку, чей локальный час LA
    >= 12: для KLAX это ftime 00Z (16/17 часов local), а 12Z — минимум.
    """
    selected: dict[date, tuple[datetime, float]] = {}
    for item in payload:
        value = item.get(marker)
        if value is None:
            continue
        cycle = _parse_utc(str(item["runtime"]))
        forecast_time = _parse_utc(str(item["ftime"]))
        local = forecast_time.astimezone(timeutil.LA)
        target = local.date()
        if local.hour < 12 or not (start <= target <= end):
            continue
        cutoff = timeutil.local_day_bounds(target)[0]
        if cycle >= cutoff:
            continue
        candidate = (cycle, float(value))
        if target not in selected or cycle > selected[target][0]:
            selected[target] = candidate
    return selected


def fetch_model_history(
    project_model: str, start: date, end: date
) -> dict[date, tuple[datetime, float]]:
    """Скачать IEM помесячно и объединить зачётные прогнозы."""
    iem_model, marker = IEM_MODELS[project_model]
    cursor = datetime.combine(start, time.min, tzinfo=UTC)
    stop = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    selected: dict[date, tuple[datetime, float]] = {}
    while cursor < stop:
        boundary = min(_month_after(cursor.replace(day=1)), stop)
        payload = http_get_json(_iem_url(iem_model, cursor, boundary))
        if not isinstance(payload, list):
            raise RuntimeError(f"IEM {iem_model}: ожидался JSON-массив")
        chunk = parse_iem_rows(payload, project_model, marker, start, end)
        for target, candidate in chunk.items():
            if target not in selected or candidate[0] > selected[target][0]:
                selected[target] = candidate
        log.info(
            "%s: %s..%s, API rows=%d, selected total=%d",
            project_model, cursor.date(), boundary.date(), len(payload), len(selected),
        )
        cursor = boundary
    return selected


def _ncei_url(start: date, end: date) -> str:
    params = {
        "dataset": "daily-summaries",
        "stations": NCEI_STATION,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "format": "json",
        "units": "standard",
        "includeAttributes": "true",
    }
    return f"{NCEI_ENDPOINT}?{urlencode(params)}"


def fetch_actual_history(start: date, end: date) -> dict[date, float]:
    payload = http_get_json(_ncei_url(start, end))
    if not isinstance(payload, list):
        raise RuntimeError("NCEI: ожидался JSON-массив")
    actuals = {
        date.fromisoformat(str(item["DATE"])): float(item["TMAX"])
        for item in payload
        if item.get("DATE") and item.get("TMAX") not in (None, "")
    }
    log.info("NCEI: фактический Tmax для %d суток", len(actuals))
    return actuals


def build_daily_rows(
    forecasts: dict[str, dict[date, tuple[datetime, float]]],
    actuals: dict[date, float],
) -> list[ModelDayError]:
    rows: list[ModelDayError] = []
    for model in IEM_MODELS:
        for target, (cycle, forecast_f) in sorted(forecasts[model].items()):
            if target not in actuals:
                continue
            rows.append(
                ModelDayError(
                    target_date=target,
                    model=model,
                    cycle=cycle,
                    forecast_tmax_f=forecast_f,
                    actual_tmax_f=actuals[target],
                    forecast_source=FORECAST_SOURCE,
                    actual_source=ACTUAL_SOURCE,
                )
            )
    return rows


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(
        description="Импорт архивных MOS/NBM прогнозов и фактов в model_daily_errors."
    )
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--start", type=_parse_date)
    parser.add_argument("--end", type=_parse_date)
    args = parser.parse_args()

    if args.start or args.end:
        if not (args.start and args.end):
            parser.error("--start и --end задаются вместе")
        if args.start > args.end:
            parser.error("--start не может быть позже --end")
        start, end = args.start, args.end
    else:
        end = timeutil.la_today() - timedelta(days=1)
        start = end - timedelta(days=args.days - 1)

    log.info("архивный импорт за %s..%s", start, end)
    actuals = fetch_actual_history(start, end)
    forecasts = {
        model: fetch_model_history(model, start, end) for model in IEM_MODELS
    }
    daily = build_daily_rows(forecasts, actuals)

    conn = repo.connect()
    try:
        written = repo.upsert_daily_errors(conn, daily)
    finally:
        conn.close()

    for model in IEM_MODELS:
        log.info("%s: архивных дней собрано — %d",
                 model, sum(1 for row in daily if row.model == model))
    skipped = len(daily) - written
    log.info("готово: записано %d строк%s", written,
             f", пропущено {skipped} (оперативные дни не перезаписываются)"
             if skipped else "")


if __name__ == "__main__":
    main()
