# polymarket.py — данные рынка ставок Polymarket на Tmax в Лос-Анджелесе.
# Ежедневное событие «Highest temperature in Los Angeles on <date>?» состоит из
# бинарных рынков-диапазонов («80-81°F» и т.п.); цена Yes-доли каждого — вменённая
# рынком вероятность, что Tmax попадёт в диапазон. Резолвится по Wunderground для
# станции KLAX в целых °F — та же станция и точность, что у наших прогнозов.
#
# Источник вспомогательный: данные показываются в /forecast рядом с моделями
# (ссылка на ставку + вероятности диапазонов), в БД не пишутся и в метриках не
# участвуют. Gamma API публичный, read-only, без ключа. Как и остальные модули
# sources — только скачивание и разбор, чистые данные.

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from app import config
from app.sources import ParseError, http_get_json

log = logging.getLogger(__name__)

# Slug использует английские названия месяцев независимо от локали процесса.
_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)

# Подписи диапазонов (groupItemTitle): «77°F or below», «78-79°F», «96°F or higher».
_BELOW_RE = re.compile(r"^(-?\d+)°F or below$")
_RANGE_RE = re.compile(r"^(-?\d+)-(-?\d+)°F$")
_ABOVE_RE = re.compile(r"^(-?\d+)°F or higher$")


@dataclass(frozen=True)
class TempBucket:
    """Один диапазон температуры с вменённой вероятностью (цена Yes)."""
    label: str          # подпись как на Polymarket, напр. '80-81°F'
    lo: int | None      # нижняя граница, °F (None — открыт вниз)
    hi: int | None      # верхняя граница, °F (None — открыт вверх)
    prob: float         # цена Yes-доли = вероятность диапазона, 0..1


@dataclass(frozen=True)
class TempMarket:
    """Рынок «Highest temperature in Los Angeles» на одни локальные сутки."""
    target_date: date            # календарные сутки LA, к которым относится рынок
    url: str                     # страница события на polymarket.com
    volume_usd: float            # оборот события, $
    buckets: list[TempBucket]    # по возрастанию температуры

    def bucket_for(self, tmax_f: float) -> TempBucket | None:
        """Диапазон, в который попадает прогноз tmax_f (в целых °F, как рынок)."""
        t = round(tmax_f)
        for b in self.buckets:
            if (b.lo is None or t >= b.lo) and (b.hi is None or t <= b.hi):
                return b
        return None


def event_slug(d: date) -> str:
    """Slug события за сутки d: '...-july-8-2026' (месяц словом, день без нуля)."""
    return config.POLYMARKET_SLUG_TEMPLATE.format(
        month=_MONTH_NAMES[d.month - 1], day=d.day, year=d.year
    )


def parse_bucket_label(label: str) -> tuple[int | None, int | None]:
    """Подпись диапазона -> (lo, hi); открытая граница — None."""
    if m := _BELOW_RE.match(label):
        return None, int(m.group(1))
    if m := _RANGE_RE.match(label):
        return int(m.group(1)), int(m.group(2))
    if m := _ABOVE_RE.match(label):
        return int(m.group(1)), None
    raise ParseError(f"неизвестная подпись диапазона Polymarket: {label!r}")


def _json_list(raw: Any) -> list:
    """Поле Gamma API, которое может прийти списком или JSON-строкой списка."""
    if isinstance(raw, str):
        return json.loads(raw)
    return list(raw or [])


def _yes_prob(market: dict) -> float | None:
    """Цена Yes-доли рынка-диапазона (= вероятность), если она есть в ответе."""
    try:
        outcomes = _json_list(market.get("outcomes"))
        prices = _json_list(market.get("outcomePrices"))
        return float(prices[outcomes.index("Yes")])
    except (ValueError, IndexError, TypeError):
        return None


def parse_event(payload: Any, target_date: date) -> TempMarket | None:
    """Ответ Gamma API (список событий по slug) -> TempMarket.

    Пустой список означает, что рынок на эти сутки ещё не создан, — это штатная
    ситуация (событие появляется примерно за сутки), возвращаем None.
    Нераспознанный диапазон пропускается с предупреждением (один сломанный рынок
    не прячет остальные), но если не распознан ни один — формат изменился
    целиком, и это уже ParseError.
    """
    if not payload:
        return None
    event = payload[0]
    buckets: list[TempBucket] = []
    for market in event.get("markets", []):
        label = market.get("groupItemTitle") or ""
        prob = _yes_prob(market)
        try:
            lo, hi = parse_bucket_label(label)
        except ParseError:
            log.warning("пропущен диапазон Polymarket с подписью %r", label)
            continue
        if prob is None:
            log.warning("диапазон Polymarket %r без цены Yes — пропущен", label)
            continue
        buckets.append(TempBucket(label=label, lo=lo, hi=hi, prob=prob))
    if not buckets:
        raise ParseError("в событии Polymarket не распознан ни один диапазон")
    buckets.sort(key=lambda b: b.lo if b.lo is not None else float("-inf"))

    slug = event.get("slug") or event_slug(target_date)
    return TempMarket(
        target_date=target_date,
        url=config.POLYMARKET_EVENT_URL_TEMPLATE.format(slug=slug),
        volume_usd=float(event.get("volume") or 0.0),
        buckets=buckets,
    )


def fetch_market(d: date) -> TempMarket | None:
    """Полный путь: скачать событие за сутки d и разобрать в TempMarket.

    None — рынок на эти сутки ещё не создан (не ошибка).
    """
    url = config.POLYMARKET_API_URL_TEMPLATE.format(slug=event_slug(d))
    return parse_event(http_get_json(url), d)
