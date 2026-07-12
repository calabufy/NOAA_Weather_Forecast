# metrics.py — расчёт метрик качества прогноза.
# Чистые функции над выборками из БД: MAE, ME (bias), RMSE, hit rate (|err| <= 1/2/3°F),
# max|err| и N дней с полными данными по окнам 7д/30д/сезон/год для каждой модели.
# Считается на лету при команде /errors.
#
# Модуль не обращается к БД и сети: на вход — готовые пары (дата, прогноз, факт)
# из repo.error_series, на выход — агрегаты. Ошибка дня = прогноз − факт (°F),
# знак сохраняется для bias. Сезоны — метеорологические (DJF/MAM/JJA/SON), год —
# календарный (оба правила зафиксированы, менять нельзя: иначе статистика несравнима).

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

# Порядок окон фиксирован — так же выводится в боте.
WINDOWS = ("7d", "30d", "season", "year")
HIT_THRESHOLDS_F = (1.0, 2.0, 3.0)


@dataclass(frozen=True)
class DayError:
    """Ошибка прогноза за одни сутки (полные данные: прогноз и факт)."""
    date: date
    forecast_f: float
    actual_f: float

    @property
    def error(self) -> float:
        """Ошибка со знаком, °F (прогноз − факт)."""
        return self.forecast_f - self.actual_f

    @property
    def abs_error(self) -> float:
        return abs(self.error)


@dataclass(frozen=True)
class WindowStats:
    """Агрегаты качества прогноза за одно окно.

    При отсутствии данных (n == 0) все числовые метрики — None.
    """
    window: str
    n: int
    mae: float | None = None
    bias: float | None = None            # ME, средняя ошибка со знаком
    rmse: float | None = None
    hit_rate: dict[float, float] | None = None  # порог °F -> доля дней |err| <= порог
    max_abs_error: float | None = None
    max_abs_error_date: date | None = None


def build_errors(pairs: list[tuple[date, float, float]]) -> list[DayError]:
    """Преобразовать пары repo.error_series в список DayError."""
    return [DayError(d, f, a) for d, f, a in pairs]


# --- Границы окон ----------------------------------------------------------

def _season_start(ref: date) -> date:
    """Начало текущего метеорологического сезона, содержащего ref.

    DJF начинается 1 декабря предыдущего года (если ref в январе/феврале).
    """
    m = ref.month
    if m in (12, 1, 2):
        year = ref.year if m == 12 else ref.year - 1
        return date(year, 12, 1)
    if m in (3, 4, 5):
        return date(ref.year, 3, 1)
    if m in (6, 7, 8):
        return date(ref.year, 6, 1)
    return date(ref.year, 9, 1)  # 9, 10, 11 — SON


def window_bounds(ref: date) -> dict[str, tuple[date, date]]:
    """Границы [start, end] каждого окна включительно.

    end — вчерашние сутки (ref − 1 день): за сегодня факта ещё нет. 7д/30д —
    скользящие окна нужной длины, сезон — с начала метеосезона, год — с 1 января.
    """
    end = ref - timedelta(days=1)
    return {
        "7d": (ref - timedelta(days=7), end),
        "30d": (ref - timedelta(days=30), end),
        "season": (_season_start(ref), end),
        "year": (date(ref.year, 1, 1), end),
    }


# --- Агрегация -------------------------------------------------------------

def compute_window(errors: list[DayError], window: str,
                   start: date, end: date) -> WindowStats:
    """Посчитать метрики по дням, попавшим в [start, end]."""
    sel = [e for e in errors if start <= e.date <= end]
    n = len(sel)
    if n == 0:
        return WindowStats(window=window, n=0)

    signed = [e.error for e in sel]
    abs_errs = [e.abs_error for e in sel]
    mae = sum(abs_errs) / n
    bias = sum(signed) / n
    rmse = math.sqrt(sum(x * x for x in signed) / n)
    hit_rate = {
        t: sum(1 for a in abs_errs if a <= t) / n for t in HIT_THRESHOLDS_F
    }
    worst = max(sel, key=lambda e: e.abs_error)
    return WindowStats(
        window=window, n=n, mae=mae, bias=bias, rmse=rmse, hit_rate=hit_rate,
        max_abs_error=worst.abs_error, max_abs_error_date=worst.date,
    )


def report(pairs: list[tuple[date, float, float]], ref: date) -> dict[str, WindowStats]:
    """Метрики по всем окнам для одной модели.

    pairs — из repo.error_series (дата, прогноз, факт); ref — «сегодня» по LA.
    Возвращает {имя_окна: WindowStats} в порядке WINDOWS.
    """
    errors = build_errors(pairs)
    bounds = window_bounds(ref)
    return {w: compute_window(errors, w, *bounds[w]) for w in WINDOWS}
