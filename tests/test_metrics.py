# test_metrics.py — тесты агрегаций метрик на синтетических данных:
# пропуски дней, границы сезона/года, корректность MAE/ME/RMSE/hit rate по окнам.

import math
from datetime import date

from app import metrics
from app.metrics import DayError


def _err(y, m, d, forecast, actual):
    return DayError(date(y, m, d), forecast, actual)


# --- Границы метеорологического сезона -------------------------------------

def test_season_start_covers_all_seasons():
    assert metrics._season_start(date(2026, 7, 12)) == date(2026, 6, 1)   # JJA
    assert metrics._season_start(date(2026, 4, 1)) == date(2026, 3, 1)    # MAM
    assert metrics._season_start(date(2026, 10, 5)) == date(2026, 9, 1)   # SON
    assert metrics._season_start(date(2026, 12, 20)) == date(2026, 12, 1)  # DJF
    # Январь/февраль относятся к DJF, начавшемуся 1 декабря предыдущего года.
    assert metrics._season_start(date(2026, 1, 15)) == date(2025, 12, 1)
    assert metrics._season_start(date(2026, 2, 28)) == date(2025, 12, 1)


def test_window_bounds_end_is_yesterday_and_7d_is_seven_days():
    b = metrics.window_bounds(date(2026, 7, 12))
    start7, end7 = b["7d"]
    assert end7 == date(2026, 7, 11)          # вчера
    assert start7 == date(2026, 7, 5)         # 7 суток: 5..11 июля
    assert (end7 - start7).days == 6
    assert b["year"][0] == date(2026, 1, 1)
    assert b["season"][0] == date(2026, 6, 1)


# --- Фильтрация по окнам и пропуски дней -----------------------------------

def test_windows_filter_by_date_and_ignore_future():
    ref = date(2026, 7, 12)
    errors = [
        _err(2026, 7, 11, 80, 80),   # в 7д/30д/сезон/год
        _err(2026, 7, 6, 80, 80),    # в 7д (граница)
        _err(2026, 7, 4, 80, 80),    # вне 7д, в 30д
        _err(2026, 6, 5, 80, 80),    # вне 30д (start=12 июня), в сезоне/году
        _err(2026, 3, 10, 80, 80),   # вне сезона (MAM), в году
        _err(2026, 7, 12, 80, 80),   # «сегодня» — вне всех окон (end=вчера)
    ]
    rep = metrics.report([(e.date, e.forecast_f, e.actual_f) for e in errors], ref)
    assert rep["7d"].n == 2
    assert rep["30d"].n == 3
    assert rep["season"].n == 4
    assert rep["year"].n == 5


def test_empty_window_has_none_metrics():
    rep = metrics.report([], date(2026, 7, 12))
    for w in metrics.WINDOWS:
        s = rep[w]
        assert s.n == 0
        assert s.mae is None and s.bias is None and s.rmse is None
        assert s.hit_rate is None and s.max_abs_error_date is None


# --- Корректность MAE / ME / RMSE / hit rate / max|err| ---------------------

def test_metric_values_on_known_errors():
    ref = date(2026, 7, 12)
    # Ошибки (прогноз − факт): +2, −4, 0, +1 за 8..11 июля (все в окне 7д).
    errors = [
        _err(2026, 7, 8, 82, 80),    # +2
        _err(2026, 7, 9, 76, 80),    # −4
        _err(2026, 7, 10, 80, 80),   # 0
        _err(2026, 7, 11, 81, 80),   # +1
    ]
    s = metrics.report([(e.date, e.forecast_f, e.actual_f) for e in errors], ref)["7d"]
    assert s.n == 4
    assert s.mae == (2 + 4 + 0 + 1) / 4            # 1.75
    assert s.bias == (2 - 4 + 0 + 1) / 4           # −0.25
    assert math.isclose(s.rmse, math.sqrt((4 + 16 + 0 + 1) / 4))  # sqrt(5.25)
    # Hit rate: |err| <= 1 -> дни 0,+1,(не −4,не +2)? |2|>1 -> нет; итог 2/4
    assert s.hit_rate[1.0] == 2 / 4   # err 0 и +1
    assert s.hit_rate[2.0] == 3 / 4   # + err +2
    assert s.hit_rate[3.0] == 3 / 4   # −4 всё ещё вне
    assert s.max_abs_error == 4
    assert s.max_abs_error_date == date(2026, 7, 9)


def test_report_returns_all_windows_in_order():
    rep = metrics.report([], date(2026, 7, 12))
    assert tuple(rep.keys()) == metrics.WINDOWS
