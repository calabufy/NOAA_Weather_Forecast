import json
from datetime import date, datetime, timezone
from pathlib import Path

from app.bot import formatting
from app.sources import ForecastPoint, polymarket

UTC = timezone.utc
FIXTURE = Path(__file__).parent / "fixtures" / "polymarket_la.json"


def _market() -> polymarket.TempMarket:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    market = polymarket.parse_event(payload, date(2026, 7, 18))
    assert market is not None
    return market


def test_event_slug_format():
    assert polymarket.event_slug(date(2026, 7, 18)) == (
        "highest-temperature-in-los-angeles-on-july-18-2026"
    )


def test_parse_event_buckets_and_bounds():
    market = _market()

    assert market.title == "Highest temperature in Los Angeles on July 18?"
    assert market.url.endswith("highest-temperature-in-los-angeles-on-july-18-2026")
    assert market.volume > 0

    first, last = market.buckets[0], market.buckets[-1]
    assert (first.low, first.high) == (None, 69)   # «69°F or below»
    assert (last.low, last.high) == (88, None)     # «88°F or higher»
    # Корзины упорядочены по температуре, вероятности в [0, 1].
    lows = [b.low for b in market.buckets[1:]]
    assert lows == sorted(lows)
    assert all(0.0 <= b.prob <= 1.0 for b in market.buckets)


def test_bucket_contains_rounds_to_gap():
    market = _market()
    # 77.5°F лежит в «зазоре» между корзинами 76-77 и 78-79 — округление
    # до целого обязано отнести его к 78-79.
    hits = [b.label for b in market.buckets if b.contains(77.5)]
    assert hits == ["78-79°F"]


def test_parse_event_empty_payload():
    assert polymarket.parse_event([], date(2026, 7, 18)) is None


def test_format_market_renders_bars_marks_and_link():
    market = _market()
    points = {
        "NBM": ForecastPoint(
            target_date=date(2026, 7, 18), model="NBM",
            cycle=datetime(2026, 7, 17, 12, tzinfo=UTC), tmax_f=76.6,
        ),
        "MAV": None,
    }

    text = formatting.format_market(market, points)

    assert "Polymarket: Tmax LA" in text
    assert "▇" in text
    assert "← NBM" in text          # 76.6 -> корзина 76-77
    assert "MAV" not in text        # без прогноза — без маркера
    assert '<a href="https://polymarket.com/event/' in text
