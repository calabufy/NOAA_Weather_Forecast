"""Импорт годовой истории MOS/NBM и метрик в отдельные архивные таблицы.

Прогнозы: Iowa Environmental Mesonet MOS Archive (исходные продукты NWS).
Факт: NOAA NCEI Daily Summaries, KLAX/GHCN station USW00023174.

Оперативные таблицы forecasts/actuals не читаются и не изменяются.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlencode

from app import metrics, timeutil
from app.db import repo
from app.db.historical import HistoricalModelDay, HistoricalModelMetric
from app.sources import http_get_json

UTC = timezone.utc
log = logging.getLogger("backfill_historical_metrics")

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
) -> list[HistoricalModelDay]:
    rows: list[HistoricalModelDay] = []
    for model in IEM_MODELS:
        for target, (cycle, forecast_f) in sorted(forecasts[model].items()):
            if target not in actuals:
                continue
            rows.append(
                HistoricalModelDay(
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


def build_metric_rows(
    daily: list[HistoricalModelDay], period_start: date, period_end: date
) -> list[HistoricalModelMetric]:
    pairs_by_model = {
        model: [
            (row.target_date, row.forecast_tmax_f, row.actual_tmax_f)
            for row in daily
            if row.model == model
        ]
        for model in IEM_MODELS
    }
    return build_metric_rows_from_pairs(pairs_by_model, period_start, period_end)


def build_metric_rows_from_pairs(
    pairs_by_model: dict[str, list[tuple[date, float, float]]],
    period_start: date,
    period_end: date,
) -> list[HistoricalModelMetric]:
    """Собрать агрегаты из уже сохранённых архивных пар."""
    out: list[HistoricalModelMetric] = []
    for model in IEM_MODELS:
        errors = [
            metrics.DayError(target, forecast_f, actual_f)
            for target, forecast_f, actual_f in pairs_by_model[model]
        ]
        stats = metrics.compute_window(errors, "archive", period_start, period_end)
        if stats.n == 0:
            log.warning("%s: нет полных архивных пар, метрика не записана", model)
            continue
        assert stats.mae is not None
        assert stats.bias is not None
        assert stats.rmse is not None
        assert stats.hit_rate is not None
        assert stats.max_abs_error is not None
        assert stats.max_abs_error_date is not None
        out.append(
            HistoricalModelMetric(
                model=model,
                period_start=period_start,
                period_end=period_end,
                n=stats.n,
                mae=stats.mae,
                bias=stats.bias,
                rmse=stats.rmse,
                hit_rate_1f=stats.hit_rate[1.0],
                hit_rate_2f=stats.hit_rate[2.0],
                hit_rate_3f=stats.hit_rate[3.0],
                max_abs_error=stats.max_abs_error,
                max_abs_error_date=stats.max_abs_error_date,
                forecast_source=FORECAST_SOURCE,
                actual_source=ACTUAL_SOURCE,
            )
        )
    return out


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(
        description="Импорт архивных MOS/NBM прогнозов, фактов и метрик."
    )
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--start", type=_parse_date)
    parser.add_argument("--end", type=_parse_date)
    parser.add_argument(
        "--metrics-only",
        action="store_true",
        help="не скачивать архив, пересчитать агрегаты из historical_model_daily",
    )
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

    if args.metrics_only:
        conn = repo.connect()
        try:
            pairs_by_model = {
                model: repo.historical_error_series(conn, model, start, end)
                for model in IEM_MODELS
            }
            available_dates = [
                target for pairs in pairs_by_model.values() for target, _, _ in pairs
            ]
            aggregate_end = max(available_dates, default=end)
            metric_rows = build_metric_rows_from_pairs(
                pairs_by_model, start, aggregate_end
            )
            repo.upsert_historical_metrics(conn, metric_rows)
        finally:
            conn.close()
        for row in metric_rows:
            log.info(
                "%s %s..%s: n=%d MAE=%.2f bias=%+.2f RMSE=%.2f "
                "hit<=1/2/3F=%.1f/%.1f/%.1f%% max=%.1fF (%s)",
                row.model, row.period_start, row.period_end,
                row.n, row.mae, row.bias, row.rmse,
                row.hit_rate_1f * 100, row.hit_rate_2f * 100,
                row.hit_rate_3f * 100, row.max_abs_error,
                row.max_abs_error_date,
            )
        return

    log.info("архивный импорт за %s..%s", start, end)
    actuals = fetch_actual_history(start, end)
    forecasts = {
        model: fetch_model_history(model, start, end) for model in IEM_MODELS
    }
    daily = build_daily_rows(forecasts, actuals)
    aggregate_end = max((row.target_date for row in daily), default=end)
    metric_rows = build_metric_rows(daily, start, aggregate_end)

    conn = repo.connect()
    try:
        repo.init_db(conn)
        repo.upsert_historical_days(conn, daily)
        repo.upsert_historical_metrics(conn, metric_rows)
    finally:
        conn.close()

    for row in metric_rows:
        log.info(
            "%s %s..%s: n=%d MAE=%.2f bias=%+.2f RMSE=%.2f hit<=2F=%.1f%%",
            row.model, row.period_start, row.period_end, row.n,
            row.mae, row.bias, row.rmse, row.hit_rate_2f * 100,
        )
    log.info("готово: historical_model_daily=%d, metrics=%d", len(daily), len(metric_rows))


if __name__ == "__main__":
    main()
