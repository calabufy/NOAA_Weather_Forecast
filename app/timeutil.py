# timeutil.py — работа с «климатическими сутками» станции LA.
# Определяет границы локальных суток (local midnight -> midnight) с учётом DST,
# вычисляет target-дату «завтра» и правило отнесения цикла модели к дате.
# Используется джобами и метриками, чтобы все «сутки» считались одинаково.
#
# Все функции чистые (без сети/БД). «Сутки» — календарные сутки в зоне станции
# (America/Los_Angeles), корректно на переходах летнего времени (DST).

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import TZ

LA = ZoneInfo(TZ)
UTC = timezone.utc


def now_la() -> datetime:
    """Текущий момент в зоне станции (aware)."""
    return datetime.now(LA)


def la_today(ref: datetime | None = None) -> date:
    """Локальная дата LA «сейчас» (или на момент ref)."""
    ref = ref or now_la()
    return ref.astimezone(LA).date()


def la_tomorrow(ref: datetime | None = None) -> date:
    """Target-дата прогноза: следующие локальные сутки LA."""
    return la_today(ref) + timedelta(days=1)


def local_day_bounds(d: date) -> tuple[datetime, datetime]:
    """Границы локальных суток d как [start, end) в aware-UTC.

    start = local midnight даты d, end = local midnight следующего дня.
    Возвращается в UTC — удобно подставлять в запрос наблюдений METAR.
    Корректно на DST: длина суток может быть 23 или 25 часов.
    """
    start_local = datetime(d.year, d.month, d.day, tzinfo=LA)
    # Пересобираем end из календарной даты, чтобы DST-переход учёлся зоной,
    # а не механическим прибавлением 24 часов.
    nd = d + timedelta(days=1)
    end_local = datetime(nd.year, nd.month, nd.day, tzinfo=LA)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def utc_to_la_date(dt: datetime) -> date:
    """Локальная дата LA, которой принадлежит момент dt.

    dt может быть naive (трактуется как UTC) или aware. Используется для
    группировки колонок бюллетеня и наблюдений по климатическим суткам.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(LA).date()


def parse_cycle(date_str: str, cycle: str) -> datetime:
    """Момент выпуска цикла модели (aware-UTC).

    date_str — YYYYMMDD, cycle — '00'|'06'|'12'|'18'.
    """
    d = datetime.strptime(date_str, "%Y%m%d").date()
    return datetime(d.year, d.month, d.day, int(cycle), tzinfo=UTC)
