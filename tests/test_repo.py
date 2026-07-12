# test_repo.py — тесты репозитория: идемпотентность записи прогнозов/фактов и
# правило выбора «зачётного» прогноза (последний цикл до local midnight target-даты).

from datetime import date, datetime, timezone

import pytest

from app.db import repo
from app.sources import ActualTmax, ForecastPoint

UTC = timezone.utc


@pytest.fixture
def conn():
    c = repo.connect(":memory:")
    yield c
    c.close()


def _fp(target, cycle, tmax, model="NBM"):
    return ForecastPoint(target_date=target, model=model, cycle=cycle, tmax_f=tmax)


# --- Идемпотентность прогнозов ---------------------------------------------

def test_forecast_upsert_is_idempotent(conn):
    cycle = datetime(2026, 7, 11, 12, tzinfo=UTC)
    repo.upsert_forecast(conn, _fp(date(2026, 7, 12), cycle, 84.0))
    repo.upsert_forecast(conn, _fp(date(2026, 7, 12), cycle, 84.0))
    n = conn.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0]
    assert n == 1


def test_forecast_reupsert_updates_tmax(conn):
    cycle = datetime(2026, 7, 11, 12, tzinfo=UTC)
    repo.upsert_forecast(conn, _fp(date(2026, 7, 12), cycle, 84.0))
    repo.upsert_forecast(conn, _fp(date(2026, 7, 12), cycle, 86.0))  # уточнённый сбор
    fc = repo.official_forecast(conn, date(2026, 7, 12), "NBM")
    assert fc is not None and fc.tmax_f == 86.0


# --- «Зачётный» прогноз: последний цикл до local midnight target-даты -------

def test_official_forecast_picks_latest_cycle_before_local_midnight(conn):
    target = date(2026, 7, 12)
    # Полночь 12 июля по LA (PDT, UTC-7) = 2026-07-12T07:00Z. «Зачётны» циклы
    # строго до неё; 07Z того же дня — уже после и не должен выбираться.
    for cyc, tmax in [
        (datetime(2026, 7, 11, 0, tzinfo=UTC), 80.0),
        (datetime(2026, 7, 11, 12, tzinfo=UTC), 82.0),
        (datetime(2026, 7, 12, 0, tzinfo=UTC), 84.0),   # 00Z 12 июля = 17:00 PDT 11-го — до полуночи
        (datetime(2026, 7, 12, 7, tzinfo=UTC), 99.0),   # ровно local midnight — исключается
        (datetime(2026, 7, 12, 12, tzinfo=UTC), 88.0),  # после полуночи — исключается
    ]:
        repo.upsert_forecast(conn, _fp(target, cyc, tmax))

    fc = repo.official_forecast(conn, target, "NBM")
    assert fc is not None
    assert fc.tmax_f == 84.0
    assert fc.cycle == datetime(2026, 7, 12, 0, tzinfo=UTC)


def test_official_forecast_isolates_model(conn):
    target = date(2026, 7, 12)
    cycle = datetime(2026, 7, 11, 12, tzinfo=UTC)
    repo.upsert_forecast(conn, _fp(target, cycle, 82.0, model="NBM"))
    repo.upsert_forecast(conn, _fp(target, cycle, 85.0, model="MAV"))
    assert repo.official_forecast(conn, target, "NBM").tmax_f == 82.0
    assert repo.official_forecast(conn, target, "MAV").tmax_f == 85.0


def test_official_forecast_none_when_no_eligible_cycle(conn):
    target = date(2026, 7, 12)
    # Единственный цикл вышел уже после local midnight target-даты.
    repo.upsert_forecast(conn, _fp(target, datetime(2026, 7, 12, 12, tzinfo=UTC), 88.0))
    assert repo.official_forecast(conn, target, "NBM") is None


# --- Факты: приоритет CLI над METAR ----------------------------------------

def test_metar_then_cli_overwrites(conn):
    d = date(2026, 7, 10)
    repo.upsert_actual(conn, ActualTmax(date=d, tmax_f=73.9, source="METAR"))
    written = repo.upsert_actual(conn, ActualTmax(date=d, tmax_f=74.0, source="CLI"))
    assert written
    got = repo.get_actual(conn, d)
    assert got.source == "CLI" and got.tmax_f == 74.0


def test_cli_not_overwritten_by_metar(conn):
    d = date(2026, 7, 10)
    repo.upsert_actual(conn, ActualTmax(date=d, tmax_f=74.0, source="CLI"))
    written = repo.upsert_actual(conn, ActualTmax(date=d, tmax_f=73.0, source="METAR"))
    assert not written  # METAR не должен затирать канонический CLI
    got = repo.get_actual(conn, d)
    assert got.source == "CLI" and got.tmax_f == 74.0


def test_metar_refreshes_metar(conn):
    d = date(2026, 7, 10)
    repo.upsert_actual(conn, ActualTmax(date=d, tmax_f=72.0, source="METAR"))
    repo.upsert_actual(conn, ActualTmax(date=d, tmax_f=73.5, source="METAR"))  # добор наблюдений
    got = repo.get_actual(conn, d)
    assert got.source == "METAR" and got.tmax_f == 73.5


def test_get_actual_none_when_absent(conn):
    assert repo.get_actual(conn, date(2026, 1, 1)) is None
