# polymarket.py — дневной рынок «Highest temperature in Los Angeles» на Polymarket.
# Событие с корзинами температур (шаг 2°F, крайние открыты: «69°F or below» /
# «84°F or higher»); цена Yes корзины = вероятность рынка. Рынок судится по
# станции Los Angeles International Airport — той же, что и прогноз бота (KLAX),
# поэтому корзины напрямую сопоставимы с Tmax моделей.
#
# Модуль в стиле sources: скачивание + парсинг, без записи в БД. В отличие от
# NOAA-источников — БЕЗ tenacity-ретраев и с коротким таймаутом: единственный
# потребитель — интерактивный /forecast, которому лучше быстро ответить без
# блока рынка, чем ждать экспоненциальный бэкофф (правило №9 в AGENTS.md).

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date

import httpx

from app import config

log = logging.getLogger(__name__)

_BELOW_RE = re.compile(r"^(\d+)°F or below$")
_RANGE_RE = re.compile(r"^(\d+)-(\d+)°F$")
_ABOVE_RE = re.compile(r"^(\d+)°F or (?:higher|above)$")
_EXACT_RE = re.compile(r"^(\d+)°F$")


@dataclass(frozen=True)
class TempBucket:
    """Одна корзина рынка: подпись, границы (включительно) и вероятность."""

    label: str          # '76-77°F', '69°F or below', '84°F or higher'
    low: int | None     # нижняя граница; None = открыта вниз
    high: int | None    # верхняя граница; None = открыта вверх
    prob: float         # цена Yes (0..1) = вероятность рынка

    def contains(self, tmax_f: float) -> bool:
        """Попадает ли прогноз в корзину (после округления до целых °F).

        Округление обязательно: корзины целочисленные с зазорами (76-77, 78-79),
        и дробный прогноз 77.5°F без округления не попал бы никуда.
        """
        t = round(tmax_f)
        return ((self.low is None or t >= self.low)
                and (self.high is None or t <= self.high))


@dataclass(frozen=True)
class TempMarket:
    """Рынок Tmax LA на конкретную дату: корзины + метаданные для показа."""

    target_date: date
    title: str
    url: str            # публичная страница события на polymarket.com
    volume: float       # оборот события, $
    buckets: list[TempBucket]


def event_slug(d: date) -> str:
    """Слаг дневного события, напр. highest-temperature-in-los-angeles-on-july-18-2026."""
    return config.POLYMARKET_SLUG_TEMPLATE.format(
        month=d.strftime("%B").lower(), day=d.day, year=d.year
    )


def _parse_bounds(label: str) -> tuple[int | None, int | None] | None:
    """Подпись корзины -> (low, high); None — формат не распознан."""
    if m := _BELOW_RE.match(label):
        return None, int(m.group(1))
    if m := _RANGE_RE.match(label):
        return int(m.group(1)), int(m.group(2))
    if m := _ABOVE_RE.match(label):
        return int(m.group(1)), None
    if m := _EXACT_RE.match(label):
        return int(m.group(1)), int(m.group(1))
    return None


def _yes_price(market: dict) -> float | None:
    """Цена исхода Yes; outcomePrices/outcomes приходят JSON-строками."""
    outcomes = market.get("outcomes") or "[]"
    prices = market.get("outcomePrices") or "[]"
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(prices, str):
        prices = json.loads(prices)
    try:
        return float(prices[outcomes.index("Yes")])
    except (ValueError, IndexError):
        return None


def parse_event(payload: list, d: date) -> TempMarket | None:
    """Ответ /events?slug=... -> TempMarket; None, если события/корзин нет."""
    if not payload:
        return None
    event = payload[0]
    buckets: list[TempBucket] = []
    for market in event.get("markets", []):
        label = market.get("groupItemTitle") or ""
        bounds = _parse_bounds(label)
        prob = _yes_price(market)
        if bounds is None or prob is None:
            log.warning("polymarket: корзина %r не распознана — пропущена", label)
            continue
        buckets.append(TempBucket(label=label, low=bounds[0], high=bounds[1], prob=prob))
    if not buckets:
        return None
    # Порядок показа — по температуре; открытая вниз корзина всегда первая.
    buckets.sort(key=lambda b: b.low if b.low is not None else -(10 ** 6))
    slug = event.get("slug") or event_slug(d)
    return TempMarket(
        target_date=d,
        title=event.get("title") or "",
        url=config.POLYMARKET_PAGE_URL.format(slug=slug),
        volume=float(event.get("volume") or 0.0),
        buckets=buckets,
    )


def fetch_market(d: date) -> TempMarket | None:
    """Скачать рынок Tmax LA на дату d; None, если события (ещё) нет."""
    url = config.POLYMARKET_EVENTS_URL.format(slug=event_slug(d))
    resp = httpx.get(
        url,
        timeout=config.POLYMARKET_TIMEOUT,
        headers={"User-Agent": config.HTTP_USER_AGENT},
        follow_redirects=True,
    )
    resp.raise_for_status()
    return parse_event(resp.json(), d)
