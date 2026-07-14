# formatting.py — форматирование ответов бота.
# Конвертация °F -> °C для отображения и сборка компактных моноширинных таблиц
# метрик. Отделено от handlers, чтобы форматирование можно было тестировать.
#
# Модуль чистый: не импортирует aiogram и БД, работает только над доменными
# объектами (ForecastPoint, WindowStats) и датами. Вывод — строки с HTML-разметкой
# Telegram (parse_mode=HTML); таблицы заворачиваются в <pre> (моноширинный блок
# со скроллом по горизонтали, поэтому ширину колонок можно не ужимать до предела).

from __future__ import annotations

import html
from datetime import date, datetime

from app import config
from app.metrics import HIT_THRESHOLDS_F, WINDOWS, WindowStats
from app.sources import ForecastPoint

# Человекочитаемые подписи окон (порядок совпадает с metrics.WINDOWS).
WINDOW_LABELS = {"7d": "7д", "30d": "30д", "season": "сез", "year": "год"}


# --- Единицы и мелкие подписи ----------------------------------------------

def f_to_c(f: float) -> float:
    """°F -> °C (хранение в °F, показ — оба)."""
    return (f - 32.0) * 5.0 / 9.0


def temp_label(f: float) -> str:
    """'84°F (28.9°C)' — температура в обеих шкалах."""
    return f"{round(f)}°F ({f_to_c(f):.1f}°C)"


def cycle_label(cycle: datetime) -> str:
    """Момент цикла модели -> '12Z' (час выпуска в UTC)."""
    return f"{cycle.hour:02d}Z"


def date_label(d: date) -> str:
    """Локальная дата -> 'Sun 12 Jul' (как в примере README)."""
    return d.strftime("%a %d %b")


# --- /forecast -------------------------------------------------------------

def format_forecast(
    target_date: date, points: dict[str, ForecastPoint | None]
) -> str:
    """Прогноз Tmax на завтра по моделям.

    points — {модель: ForecastPoint | None}; None означает «прогноз ещё не
    собран» (честное сообщение вместо пустоты). Порядок моделей — как в
    переданном словаре (вызывающий формирует его по config.BOT_MODELS).
    """
    head = f"Прогноз Tmax на завтра ({date_label(target_date)}, {config.STATION}):"
    lines = [f"<b>{html.escape(head)}</b>"]
    for model, fp in points.items():
        if fp is None:
            lines.append(f"{model}: —  (прогноз ещё не собран)")
        else:
            lines.append(
                f"{model}: {temp_label(fp.tmax_f)}, цикл {cycle_label(fp.cycle)}"
            )
    return "\n".join(lines)


# --- /errors ---------------------------------------------------------------

def _cell(value: float | None, *, signed: bool = False) -> str:
    """Число или '—' при отсутствии данных; signed добавляет знак (для bias)."""
    if value is None:
        return "—"
    return f"{value:+.1f}" if signed else f"{value:.1f}"


def _hit_cells(stats: WindowStats) -> list[str]:
    """Доли hit-rate по фиксированным порогам как целые проценты ('83' / '—')."""
    if stats.hit_rate is None:
        return ["—"] * len(HIT_THRESHOLDS_F)
    return [f"{round(stats.hit_rate[t] * 100)}" for t in HIT_THRESHOLDS_F]


def _max_cell(stats: WindowStats) -> str:
    """max|err| и его дата: '5.2 08.07' или '—'."""
    if stats.max_abs_error is None or stats.max_abs_error_date is None:
        return "—"
    return f"{stats.max_abs_error:.1f} {stats.max_abs_error_date.strftime('%d.%m')}"


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    """Моноширинная таблица: ширина колонки — по максимуму содержимого, ячейки
    выравниваются по правому краю, разделитель — два пробела.
    """
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: list[str]) -> str:
        return "  ".join(c.rjust(widths[i]) for i, c in enumerate(cells))

    return "\n".join([fmt_row(headers), *(fmt_row(r) for r in rows)])


def format_model_errors(model: str, report: dict[str, WindowStats]) -> str:
    """Одна моноширинная таблица метрик для модели (все окна)."""
    headers = ["Окно", "N", "MAE", "ME", "RMSE", "≤1", "≤2", "≤3", "max|err|"]
    rows: list[list[str]] = []
    for w in WINDOWS:
        s = report[w]
        rows.append([
            WINDOW_LABELS[w],
            str(s.n),
            _cell(s.mae),
            _cell(s.bias, signed=True),
            _cell(s.rmse),
            *_hit_cells(s),
            _max_cell(s),
        ])
    table = _render_table(headers, rows)
    return f"<b>{model}</b>\n<pre>{html.escape(table)}</pre>"


def format_errors(reports: dict[str, dict[str, WindowStats]]) -> str:
    """Сводка ошибок по всем моделям.

    reports — {модель: {окно: WindowStats}} (из metrics.report на каждую модель).
    Каждая модель — своя таблица; внизу — легенда столбцов.
    """
    blocks = [format_model_errors(m, reports[m]) for m in reports]
    legend = (
        "MAE/ME/RMSE — °F; ME — систематическое смещение (прогноз−факт);\n"
        "≤1/≤2/≤3 — доля дней |err|≤N°F, %; N — дней с полными данными."
    )
    return "\n\n".join(blocks) + "\n\n" + legend
