# verify.py — Verification Job.
# Утром по LA-времени забирает фактический Tmax за вчера из CLI-отчёта и пишет
# в actuals. Если CLI ещё нет — fallback на METAR (source='METAR') с перезаписью
# на CLI позже (CLI приоритетнее — правило реализовано в repo.upsert_actual).
#
# Джоб идемпотентен и устойчив к сбоям: если ни CLI, ни METAR не дали факта,
# просто логируем — повторная попытка (по расписанию) доберёт данные позже.

from __future__ import annotations

import logging
from datetime import date, timedelta

from app import timeutil
from app.db import repo
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


def run(conn, target_date: date | None = None) -> ActualTmax | None:
    """Записать фактический Tmax за сутки target_date (по умолчанию — вчера).

    Приоритет: CLI (канонический) -> METAR (fallback). Запись через
    repo.upsert_actual, который не даст METAR затереть уже записанный CLI.
    """
    d = target_date or (timeutil.la_today() - timedelta(days=1))
    actual = _try_cli(d) or _try_metar(d)
    if actual is None:
        log.warning("факт за %s пока не получен (ни CLI, ни METAR)", d.isoformat())
        return None
    written = repo.upsert_actual(conn, actual)
    log.info("факт за %s: %.1f°F (%s)%s", d.isoformat(), actual.tmax_f,
             actual.source, "" if written else " — не записан (приоритет CLI)")
    return actual
