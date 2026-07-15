from datetime import date, datetime, timezone

from app.db import repo
from app.jobs import fetch_forecasts, verify
from app.sources import ActualTmax, ForecastPoint

UTC = timezone.utc


def test_latest_cycle_applies_lag_and_rounds_down():
    assert fetch_forecasts.latest_cycle(
        datetime(2026, 7, 16, 10, tzinfo=UTC)
    ) == (date(2026, 7, 16), "06")


def test_latest_cycle_lag_can_cross_utc_midnight():
    assert fetch_forecasts.latest_cycle(
        datetime(2026, 7, 16, 2, tzinfo=UTC)
    ) == (date(2026, 7, 15), "18")


def test_verify_prefers_matching_cli(monkeypatch):
    conn = repo.connect(":memory:")
    target = date(2026, 7, 15)
    cli = ActualTmax(target, 74.0, "CLI")
    monkeypatch.setattr(verify, "_try_cli", lambda d: cli)
    monkeypatch.setattr(
        verify, "_try_metar", lambda d: (_ for _ in ()).throw(AssertionError())
    )
    try:
        assert verify.run(conn, target) == cli
        assert repo.get_actual(conn, target) == cli
    finally:
        conn.close()


def test_verify_falls_back_to_metar(monkeypatch):
    conn = repo.connect(":memory:")
    target = date(2026, 7, 15)
    metar = ActualTmax(target, 73.5, "METAR")
    monkeypatch.setattr(verify, "_try_cli", lambda d: None)
    monkeypatch.setattr(verify, "_try_metar", lambda d: metar)
    try:
        assert verify.run(conn, target) == metar
    finally:
        conn.close()


def test_verify_returns_none_when_both_sources_fail(monkeypatch):
    conn = repo.connect(":memory:")
    monkeypatch.setattr(verify, "_try_cli", lambda d: None)
    monkeypatch.setattr(verify, "_try_metar", lambda d: None)
    try:
        assert verify.run(conn, date(2026, 7, 15)) is None
    finally:
        conn.close()


def test_fetch_job_writes_successful_models_after_partial_failure(monkeypatch):
    conn = repo.connect(":memory:")
    target = date(2026, 7, 17)
    cycle = datetime(2026, 7, 16, 12, tzinfo=UTC)
    point = ForecastPoint(target, "NBM", cycle, 81.0)
    monkeypatch.setattr(
        fetch_forecasts,
        "fetch_all_isolated",
        lambda d, c: {"NBM": [point], "MAV": [], "MET": []},
    )
    try:
        counts = fetch_forecasts.run(conn, date(2026, 7, 16), "12")
        assert counts == {"NBM": 1, "MAV": 0, "MET": 0}
        assert repo.latest_forecast(conn, target, "NBM") == point
    finally:
        conn.close()
