from datetime import date, datetime, timezone

from app.db import sqlite_repo
from app.db.daily_errors import OPERATIONAL_SOURCE, ModelDayError
from scripts.backfill_daily_errors import (
    ACTUAL_SOURCE,
    FORECAST_SOURCE,
    parse_iem_rows,
)

UTC = timezone.utc


def _archive_row(tmax: float = 74.0) -> ModelDayError:
    return ModelDayError(
        target_date=date(2025, 7, 16),
        model="MAV",
        cycle=datetime(2025, 7, 16, 6, tzinfo=UTC),
        forecast_tmax_f=tmax,
        actual_tmax_f=73.0,
        forecast_source=FORECAST_SOURCE,
        actual_source=ACTUAL_SOURCE,
    )


def _operational_row(tmax: float = 75.0) -> ModelDayError:
    return ModelDayError(
        target_date=date(2025, 7, 16),
        model="MAV",
        cycle=datetime(2025, 7, 16, 12, tzinfo=UTC),
        forecast_tmax_f=tmax,
        actual_tmax_f=76.0,
        forecast_source=OPERATIONAL_SOURCE,
        actual_source="CLI",
    )


def test_iem_parser_selects_last_cycle_before_la_midnight():
    payload = [
        # 00Z ftime = 17:00 PDT previous local date: дневной максимум.
        {"runtime": "2025-07-16T00:00:00", "ftime": "2025-07-17T00:00:00", "n_x": 73},
        {"runtime": "2025-07-16T06:00:00", "ftime": "2025-07-17T00:00:00", "n_x": 74},
        # Цикл после local midnight (07Z) не может быть зачётным.
        {"runtime": "2025-07-16T12:00:00", "ftime": "2025-07-17T00:00:00", "n_x": 75},
        # 12Z ftime = 05:00 PDT: это ночной минимум, а не Tmax.
        {"runtime": "2025-07-16T06:00:00", "ftime": "2025-07-17T12:00:00", "n_x": 61},
    ]

    selected = parse_iem_rows(
        payload, "MAV", "n_x", date(2025, 7, 16), date(2025, 7, 16)
    )

    assert selected == {
        date(2025, 7, 16): (datetime(2025, 7, 16, 6, tzinfo=UTC), 74.0)
    }


def test_daily_errors_do_not_touch_live_tables():
    conn = sqlite_repo.connect(":memory:")

    assert sqlite_repo.upsert_daily_errors(conn, [_archive_row()]) == 1
    assert sqlite_repo.daily_error_series(
        conn, "MAV", date(2025, 7, 16), date(2025, 7, 16)
    ) == [(date(2025, 7, 16), 74.0, 73.0)]
    assert conn.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM actuals").fetchone()[0] == 0


def test_archive_does_not_overwrite_operational_row():
    conn = sqlite_repo.connect(":memory:")
    assert sqlite_repo.upsert_daily_errors(conn, [_operational_row()]) == 1

    # Архивная строка на ту же (дату, модель) молча пропускается.
    assert sqlite_repo.upsert_daily_errors(conn, [_archive_row()]) == 0
    series = sqlite_repo.daily_error_series(
        conn, "MAV", date(2025, 7, 16), date(2025, 7, 16)
    )
    assert series == [(date(2025, 7, 16), 75.0, 76.0)]


def test_operational_overwrites_any_row():
    conn = sqlite_repo.connect(":memory:")
    assert sqlite_repo.upsert_daily_errors(conn, [_archive_row()]) == 1

    # Оперативная строка (повторная верификация) перезаписывает архивную...
    assert sqlite_repo.upsert_daily_errors(conn, [_operational_row(75.0)]) == 1
    # ...и другую оперативную (уточнение факта METAR -> CLI).
    assert sqlite_repo.upsert_daily_errors(conn, [_operational_row(77.0)]) == 1
    series = sqlite_repo.daily_error_series(
        conn, "MAV", date(2025, 7, 16), date(2025, 7, 16)
    )
    assert series == [(date(2025, 7, 16), 77.0, 76.0)]
