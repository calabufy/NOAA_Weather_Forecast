# test_formatting.py — тесты форматирования ответов бота (чистый слой, без aiogram):
# конвертация °F->°C, подписи цикла/даты/температуры, вывод /forecast и таблиц /errors.

from datetime import date, datetime, timezone

from app.bot import formatting
from app.metrics import WindowStats, report
from app.sources import ForecastPoint

UTC = timezone.utc


# --- Единицы и подписи ------------------------------------------------------

def test_f_to_c_and_temp_label():
    assert formatting.f_to_c(32.0) == 0.0
    assert round(formatting.f_to_c(212.0), 6) == 100.0
    # 84°F -> 28.9°C, температура округляется до целого °F.
    assert formatting.temp_label(84.0) == "84°F (28.9°C)"


def test_cycle_label_is_utc_hour():
    assert formatting.cycle_label(datetime(2026, 7, 11, 12, tzinfo=UTC)) == "12Z"
    assert formatting.cycle_label(datetime(2026, 7, 11, 0, tzinfo=UTC)) == "00Z"


# --- /forecast --------------------------------------------------------------

def _fp(model, cycle_hour, tmax):
    return ForecastPoint(
        target_date=date(2026, 7, 13),
        model=model,
        cycle=datetime(2026, 7, 12, cycle_hour, tzinfo=UTC),
        tmax_f=tmax,
    )


def test_format_forecast_lists_models_with_cycle():
    points = {"NBM": _fp("NBM", 12, 84.0), "MAV": _fp("MAV", 12, 85.0)}
    out = formatting.format_forecast(date(2026, 7, 13), points)
    assert "NBM: 84°F (28.9°C), цикл 12Z" in out
    assert "MAV: 85°F (29.4°C), цикл 12Z" in out
    assert "KLAX" in out


def test_format_forecast_reports_missing_model_honestly():
    out = formatting.format_forecast(date(2026, 7, 13), {"NBM": None})
    assert "NBM: —" in out
    assert "ещё не собран" in out


# --- /errors ----------------------------------------------------------------

def test_empty_window_renders_dashes():
    out = formatting.format_model_errors("NBM", {
        w: WindowStats(window=w, n=0) for w in ("7d", "30d", "season", "year")
    })
    assert "<b>NBM</b>" in out
    assert "<pre>" in out
    assert "—" in out  # метрики отсутствуют


def test_format_errors_end_to_end_from_report():
    # Три дня подряд с ошибками +2, 0, -1 °F. ref=13-е, окно 7д включает 10..12.
    pairs = [
        (date(2026, 7, 10), 84.0, 82.0),
        (date(2026, 7, 11), 80.0, 80.0),
        (date(2026, 7, 12), 79.0, 80.0),
    ]
    reports = {"NBM": report(pairs, date(2026, 7, 13))}
    out = formatting.format_errors(reports)
    assert "<b>NBM</b>" in out
    assert "7д" in out and "год" in out
    # N=3 в окне 7д; MAE=(2+0+1)/3=1.0.
    assert "3" in out
    assert "1.0" in out
    # Легенда присутствует.
    assert "MAE" in out and "ME" in out
