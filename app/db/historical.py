"""Типы для изолированного архива интернет-прогнозов и метрик."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class HistoricalModelDay:
    """Зачётный архивный прогноз модели и факт за одни локальные сутки LA."""

    target_date: date
    model: str
    cycle: datetime
    forecast_tmax_f: float
    actual_tmax_f: float
    forecast_source: str
    actual_source: str

    @property
    def error_f(self) -> float:
        return self.forecast_tmax_f - self.actual_tmax_f

    @property
    def abs_error_f(self) -> float:
        return abs(self.error_f)


@dataclass(frozen=True)
class HistoricalModelMetric:
    """Агрегированные метрики одной модели за архивный период."""

    model: str
    period_start: date
    period_end: date
    n: int
    mae: float
    bias: float
    rmse: float
    hit_rate_1f: float
    hit_rate_2f: float
    hit_rate_3f: float
    max_abs_error: float
    max_abs_error_date: date
    forecast_source: str
    actual_source: str
