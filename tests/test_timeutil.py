# test_timeutil.py — тесты «климатических суток» LA: границы суток, выбор target-даты,
# корректность на переходах DST.

from datetime import date, datetime, timezone

from app import timeutil

UTC = timezone.utc


def test_la_today_and_tomorrow_use_local_date():
    # 2026-07-11 06:00Z = 23:00 PDT 10 июля -> локальная дата LA = 10 июля.
    ref = datetime(2026, 7, 11, 6, 0, tzinfo=UTC)
    assert timeutil.la_today(ref) == date(2026, 7, 10)
    assert timeutil.la_tomorrow(ref) == date(2026, 7, 11)


def test_utc_to_la_date_naive_treated_as_utc():
    naive = datetime(2026, 7, 11, 6, 0)  # без tz -> трактуется как UTC
    assert timeutil.utc_to_la_date(naive) == date(2026, 7, 10)
    aware = datetime(2026, 7, 11, 20, 0, tzinfo=UTC)  # 13:00 PDT 11 июля
    assert timeutil.utc_to_la_date(aware) == date(2026, 7, 11)


def test_local_day_bounds_summer_is_24h_pdt():
    start, end = timeutil.local_day_bounds(date(2026, 7, 10))
    # Лето: PDT = UTC-7, полночь 10 июля = 07:00Z, полночь 11 июля = 07:00Z.
    assert start == datetime(2026, 7, 10, 7, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 11, 7, 0, tzinfo=UTC)
    assert (end - start).total_seconds() == 24 * 3600


def test_local_day_bounds_spring_forward_is_23h():
    # 8 марта 2026 — переход на летнее время: сутки короче на час.
    start, end = timeutil.local_day_bounds(date(2026, 3, 8))
    assert (end - start).total_seconds() == 23 * 3600


def test_local_day_bounds_fall_back_is_25h():
    # 1 ноября 2026 — возврат на зимнее время: сутки длиннее на час.
    start, end = timeutil.local_day_bounds(date(2026, 11, 1))
    assert (end - start).total_seconds() == 25 * 3600


def test_parse_cycle_builds_utc_datetime():
    dt = timeutil.parse_cycle("20260711", "12")
    assert dt == datetime(2026, 7, 11, 12, 0, tzinfo=UTC)