# fetch_forecasts.py — Forecast Fetcher.
# По расписанию (после выхода циклов 00Z/06Z/12Z/18Z) вызывает источники NBM/MAV,
# получает Tmax по локальным суткам и идемпотентно пишет в таблицу forecasts
# (ключ target_date+model+cycle).
#
# Джоб устойчив к сбоям: неудача одной модели (сеть, ParseError) логируется и
# не мешает остальным. Пишутся все дни, вернувшиеся из бюллетеня (каждый со своей
# target_date) — так «зачётный» прогноз найдётся независимо от того, когда его
# спросят. Идемпотентность гарантирует repo (upsert по ключу).

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from app import config
from app.db import repo
from app.sources import mav, nbm

UTC = timezone.utc
log = logging.getLogger(__name__)


def latest_cycle(now: datetime | None = None) -> tuple[date, str]:
    """Свежий доступный цикл модели: (UTC-дата, '00'|'06'|'12'|'18').

    Бюллетени NBS/MAV публикуются с задержкой после часа цикла, поэтому отступаем
    на config.FETCH_LAG_HOURS и округляем вниз до ближайшего 6-часового цикла.
    """
    now = now or datetime.now(UTC)
    ref = now - timedelta(hours=config.FETCH_LAG_HOURS)
    cyc_hour = (ref.hour // 6) * 6
    return ref.date(), f"{cyc_hour:02d}"


def _fetch_model(conn, model: str, points_fn) -> int:
    """Собрать и записать прогнозы одной модели; вернуть число строк.

    Любая ошибка (сеть, разбор) не пробрасывается: логируется и возвращается 0,
    чтобы сбой одной модели не рушил весь цикл сбора.
    """
    try:
        points = points_fn()
    except Exception:  # noqa: BLE001 — намеренно широко: джоб не должен падать
        log.exception("сбор прогноза %s не удался", model)
        return 0
    n = repo.upsert_forecasts(conn, points)
    log.info("записано прогнозов %s: %d (дни %s)", model, n,
             ", ".join(sorted(p.target_date.isoformat() for p in points)) or "—")
    return n


def run(conn, run_date: date | None = None, cycle: str | None = None) -> dict[str, int]:
    """Собрать NBM и MAV за (run_date, cycle) и записать в БД.

    run_date/cycle=None -> берётся свежий доступный цикл (latest_cycle).
    Возвращает {model: число записанных строк}.
    """
    if run_date is None or cycle is None:
        run_date, cycle = latest_cycle()
    log.info("сбор прогнозов за цикл %s %sZ", run_date.isoformat(), cycle)
    return {
        nbm.MODEL: _fetch_model(conn, nbm.MODEL,
                                lambda: nbm.fetch_forecast(run_date, cycle)),
        mav.MODEL: _fetch_model(conn, mav.MODEL,
                                lambda: mav.fetch_forecast(cycle)),
    }
