"""Тип строки единой таблицы дневных ошибок моделей (model_daily_errors).

Таблица хранит «зачётный» прогноз + факт + ошибку за каждые локальные сутки LA
за всё время. Два писателя:
  - scripts/backfill_daily_errors.py — интернет-архив (IEM MOS Archive + NCEI);
  - verify-джоб — оперативные дни по мере жизни бота.
Правило конфликтов: оперативная строка (forecast_source=OPERATIONAL_SOURCE)
не может быть затёрта архивной — реализовано в repo.upsert_daily_errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

# Маркер оперативных строк (пишет verify-джоб из собственных forecasts/actuals).
OPERATIONAL_SOURCE = "OPERATIONAL"


@dataclass(frozen=True)
class ModelDayError:
    """Зачётный прогноз модели и факт за одни локальные сутки LA."""

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
