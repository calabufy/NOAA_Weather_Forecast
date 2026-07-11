# Пакет sources — получение и парсинг данных NOAA.
# Каждый модуль только скачивает и разбирает бюллетень/ответ и возвращает
# чистые данные, НЕ пишет в БД (это упрощает юнит-тесты на образцах).
#
# Здесь — общие для источников dataclass'ы и тонкий HTTP-слой (httpx + tenacity).
# Парсеры лежат в модулях и вызываются на сырых строках/JSON, поэтому тестируются
# на файлах из tests/fixtures/ без обращения к сети.

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app import config
from app import timeutil

UTC = timezone.utc


# --- Доменные модели -------------------------------------------------------

@dataclass(frozen=True)
class ForecastPoint:
    """Прогноз максимальной температуры на конкретные локальные сутки LA."""
    target_date: date       # климатическая дата LA, к которой относится Tmax
    model: str              # 'NBM' | 'MAV'
    cycle: datetime         # момент выпуска цикла модели (aware-UTC)
    tmax_f: float           # прогноз Tmax, °F


@dataclass(frozen=True)
class ActualTmax:
    """Фактический Tmax суток (для верификации прогноза)."""
    date: date              # климатическая дата LA
    tmax_f: float           # факт Tmax, °F
    source: str             # 'CLI' | 'METAR'


@dataclass(frozen=True)
class Observation:
    """Одно наблюдение METAR (для fallback-расчёта факта)."""
    ts_utc: datetime        # момент наблюдения (aware-UTC)
    temp_f: float           # температура, °F


# --- Ошибки парсинга -------------------------------------------------------

class ParseError(Exception):
    """Доменная ошибка разбора бюллетеня/ответа.

    Парсеры бросают её (а не «сырые» ValueError из глубины) при неожиданном
    формате, чтобы вызывающий код мог залогировать и слать алерт, не падая.
    """


# --- Единицы ---------------------------------------------------------------

def c_to_f(celsius: float) -> float:
    """°C -> °F (нативные единицы проекта — °F)."""
    return celsius * 9.0 / 5.0 + 32.0


# --- HTTP-слой -------------------------------------------------------------

def _headers() -> dict[str, str]:
    return {"User-Agent": config.HTTP_USER_AGENT}


@retry(reraise=True, stop=stop_after_attempt(4),
       wait=wait_exponential(multiplier=1, min=2, max=30))
def http_get_text(url: str) -> str:
    """GET с ретраями, вернуть тело как текст."""
    with httpx.Client(timeout=config.HTTP_TIMEOUT, headers=_headers(),
                      follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


@retry(reraise=True, stop=stop_after_attempt(4),
       wait=wait_exponential(multiplier=1, min=2, max=30))
def http_get_json(url: str) -> Any:
    """GET с ретраями, вернуть распарсенный JSON."""
    with httpx.Client(timeout=config.HTTP_TIMEOUT, headers=_headers(),
                      follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.json()


# --- Парсер фиксированных текстовых бюллетеней (общий для NBS и MAV) --------
# Структура MOS/NBM-бюллетеня одинакова: строка-шапка со станцией/моделью/циклом,
# строка часов (UTC/HR), строка max/min (TXN/N-X), строка почасовой TMP.
# Значения выровнены по правому краю в колонках, совпадающих с колонками часов,
# поэтому парсим не токенизацией (в строке max/min есть пропуски!), а слайсами
# по позициям токенов часов.

_HEADER_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{2})(\d{2})\s+UTC")


def _find_row(lines: list[str], label: str) -> str | None:
    """Первая строка, чей первый токен равен label (напр. 'UTC', 'TMP')."""
    for ln in lines:
        toks = ln.split(None, 1)
        if toks and toks[0] == label:
            return ln
    return None


def _column_bounds(hours_row: str) -> tuple[int, list[tuple[int, int]], list[int]]:
    """По строке часов вернуть (label_end, спаны колонок, часы).

    Спан колонки — (left, right) в символах; значение в любой строке данных
    берётся как hours_row-совместимый слайс и обрезается по пробелам.
    """
    toks = list(re.finditer(r"\S+", hours_row))
    if len(toks) < 2:
        raise ParseError("не удалось разобрать строку часов бюллетеня")
    label_end = toks[0].end()
    hour_toks = toks[1:]
    rights = [m.end() for m in hour_toks]
    lefts = [label_end] + rights[:-1]
    hours = [int(m.group()) for m in hour_toks]
    return label_end, list(zip(lefts, rights)), hours


def _slice(row: str, span: tuple[int, int]) -> str:
    return row[span[0]:span[1]].strip()


def _column_datetimes(cycle_dt: datetime, hours: list[int]) -> list[datetime]:
    """Абсолютные UTC-времена колонок по часам с учётом смены суток.

    Прогноз начинается не раньше цикла; далее сутки увеличиваются каждый раз,
    когда час колонки не больше предыдущего.
    """
    first = datetime(cycle_dt.year, cycle_dt.month, cycle_dt.day,
                     hours[0], tzinfo=UTC)
    if first < cycle_dt:
        first += timedelta(days=1)
    out = [first]
    for h in hours[1:]:
        prev = out[-1]
        cur = prev.replace(hour=h)
        if h <= prev.hour:
            cur += timedelta(days=1)
        out.append(cur)
    return out


def parse_fixed_bulletin(text: str, model: str, hours_label: str,
                         maxmin_label: str) -> list[ForecastPoint]:
    """Разобрать блок фикс-формата (NBS/MAV) в прогнозы Tmax по локальным суткам.

    Основной сигнал — дневной максимум из строки max/min (TXN/N-X): значение
    относится к колонке, чей локальный час LA >= 12 (послеполуденный максимум).
    Только если строки max/min нет вовсе — фолбэк на max почасовой TMP по суткам
    (иначе неполный последний день дал бы утренний минимум вместо максимума).
    Возвращает по одному ForecastPoint на каждую локальную дату с дневным Tmax.
    """
    lines = text.splitlines()
    header = next((ln for ln in lines if _HEADER_RE.search(ln)), None)
    if header is None:
        raise ParseError("не найдена строка-шапка с датой/циклом")
    mo, da, yr, hh, _mm = _HEADER_RE.search(header).groups()
    cycle_dt = datetime(int(yr), int(mo), int(da), int(hh), tzinfo=UTC)

    hours_row = _find_row(lines, hours_label)
    tmax_row = _find_row(lines, maxmin_label)
    tmp_row = _find_row(lines, "TMP")
    if hours_row is None or tmp_row is None:
        raise ParseError("отсутствует строка часов или TMP")

    _label_end, spans, hours = _column_bounds(hours_row)
    col_dt = _column_datetimes(cycle_dt, hours)

    # Дневные максимумы из строки max/min.
    tmax_by_date: dict[date, float] = {}
    if tmax_row is not None:
        for span, dt in zip(spans, col_dt):
            raw = _slice(tmax_row, span)
            if not raw or not raw.lstrip("-").isdigit():
                continue
            local = dt.astimezone(timeutil.LA)
            if local.hour >= 12:  # послеполуденный максимум
                d = local.date()
                val = float(int(raw))
                tmax_by_date[d] = max(tmax_by_date.get(d, val), val)

    # Фолбэк только при полном отсутствии строки max/min: max почасовой TMP.
    if not tmax_by_date:
        for span, dt in zip(spans, col_dt):
            raw = _slice(tmp_row, span)
            if not raw or not raw.lstrip("-").isdigit():
                continue
            d = timeutil.utc_to_la_date(dt)
            val = float(int(raw))
            tmax_by_date[d] = max(tmax_by_date.get(d, val), val)

    return [
        ForecastPoint(target_date=d, model=model, cycle=cycle_dt,
                      tmax_f=tmax_by_date[d])
        for d in sorted(tmax_by_date)
    ]


@retry(reraise=True, stop=stop_after_attempt(4),
       wait=wait_exponential(multiplier=1, min=2, max=30))
def http_iter_lines(url: str):
    """Стрим тела построчно (для больших bulk-файлов, напр. NBS ~28 МБ).

    Возвращает генератор строк без разделителей; соединение закрывается по
    исчерпании итератора или досрочном break у вызывающего.
    """
    with httpx.Client(timeout=config.HTTP_TIMEOUT, headers=_headers(),
                      follow_redirects=True) as client:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            yield from r.iter_lines()