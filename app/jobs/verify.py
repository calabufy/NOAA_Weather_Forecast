# verify.py — Verification Job.
# Утром по LA-времени забирает фактический Tmax за вчера из CLI-отчёта и пишет
# в actuals. Если CLI ещё нет — fallback на METAR (source='METAR') с перезаписью
# на CLI позже (CLI приоритетнее — правило реализовано в repo.upsert_actual).
#
# Джоб идемпотентен и устойчив к сбоям: если ни CLI, ни METAR не дали факта,
# просто логируем — повторная попытка (по расписанию) доберёт данные позже.

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta

from app import config, timeutil
from app.db import repo
from app.db.daily_errors import OPERATIONAL_SOURCE, ModelDayError
from app.sources import ActualTmax, cli_report, nws

log = logging.getLogger(__name__)


def _try_cli(d: date) -> ActualTmax | None:
    """Свежий CLILAX, если он относится именно к суткам d; иначе None.

    CLI за вчера может ещё не выйти — тогда свежий отчёт будет за позавчера и мы
    его не принимаем (даст факт не за ту дату).
    """
    try:
        actual = cli_report.fetch_actual()
    except Exception:  # noqa: BLE001 — сбой источника не должен ронять джоб
        log.exception("получение CLI-факта не удалось")
        return None
    if actual.date != d:
        log.info("свежий CLI за %s, а нужен факт за %s — ждём выпуска",
                 actual.date.isoformat(), d.isoformat())
        return None
    return actual


def _try_metar(d: date) -> ActualTmax | None:
    """Fallback: посчитать факт за сутки d по наблюдениям METAR."""
    try:
        return nws.fetch_actual(d)
    except Exception:  # noqa: BLE001
        log.exception("получение METAR-факта за %s не удалось", d.isoformat())
        return None


def record_daily_errors(conn, d: date, actual: ActualTmax) -> int:
    """Зафиксировать дневные ошибки моделей за сутки d в model_daily_errors.

    Для каждой модели берётся «зачётный» прогноз (последний цикл до local
    midnight). Строки оперативные (OPERATIONAL_SOURCE): повторная верификация
    перезапишет их с уточнённым фактом (METAR -> CLI), а архивный бэкфилл
    затереть их не сможет (правило в repo.upsert_daily_errors).
    """
    rows = []
    for model in config.BOT_MODELS:
        fp = repo.official_forecast(conn, d, model)
        if fp is None:
            log.info("нет зачётного прогноза %s за %s — ошибка дня не записана",
                     model, d.isoformat())
            continue
        rows.append(ModelDayError(
            target_date=d, model=model, cycle=fp.cycle,
            forecast_tmax_f=fp.tmax_f, actual_tmax_f=actual.tmax_f,
            forecast_source=OPERATIONAL_SOURCE, actual_source=actual.source,
        ))
    return repo.upsert_daily_errors(conn, rows) if rows else 0


def run(
    conn: sqlite3.Connection, target_date: date | None = None
) -> ActualTmax | None:
    """Записать фактический Tmax за сутки target_date (по умолчанию — вчера).

    Приоритет: CLI (канонический) -> METAR (fallback). Запись через
    repo.upsert_actual, который не даст METAR затереть уже записанный CLI.
    После записи факта фиксируются дневные ошибки моделей (единая таблица
    model_daily_errors — из неё читает /errors).
    """
    d = target_date or (timeutil.la_today() - timedelta(days=1))
    actual = _try_cli(d) or _try_metar(d)
    if actual is None:
        log.warning("факт за %s пока не получен (ни CLI, ни METAR)", d.isoformat())
        return None
    written = repo.upsert_actual(conn, actual)
    log.info("факт за %s: %.1f°F (%s)%s", d.isoformat(), actual.tmax_f,
             actual.source, "" if written else " — не записан (приоритет CLI)")
    if written:
        n = record_daily_errors(conn, d, actual)
        log.info("дневные ошибки за %s: записано моделей — %d", d.isoformat(), n)
    return actual
