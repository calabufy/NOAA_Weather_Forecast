# test_polymarket.py — тесты источника Polymarket (app/sources/polymarket.py):
# slug события по дате, разбор подписей диапазонов, разбор ответа Gamma API на
# фикстуре, выбор диапазона для прогноза и форматирование блока в /forecast.

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.bot import formatting
from app.sources import ForecastPoint, ParseError, polymarket

FIXTURES = Path(__file__).parent / "fixtures"
TARGET = date(2026, 7, 16)


def _load_market() -> polymarket.TempMarket:
    payload = json.loads((FIXTURES / "polymarket_la.json").read_text(encoding="utf-8"))
    market = polymarket.parse_event(payload, TARGET)
    assert market is not None
    return market


# --- Slug события ------------------------------------------------------------

def test_event_slug_month_word_day_unpadded():
    # Формат подтверждён реальными URL: месяц словом, день без ведущего нуля.
    assert polymarket.event_slug(date(2026, 7, 8)) == (
        "highest-temperature-in-los-angeles-on-july-8-2026"
    )
    assert polymarket.event_slug(date(2026, 5, 1)) == (
        "highest-temperature-in-los-angeles-on-may-1-2026"
    )


# --- Подписи диапазонов --------------------------------------------------------

def test_parse_bucket_label_forms():
    assert polymarket.parse_bucket_label("77°F or below") == (None, 77)
    assert polymarket.parse_bucket_label("78-79°F") == (78, 79)
    assert polymarket.parse_bucket_label("96°F or higher") == (96, None)


def test_parse_bucket_label_rejects_garbage():
    with pytest.raises(ParseError):
        polymarket.parse_bucket_label("Rain or shine")


# --- Разбор события ------------------------------------------------------------

def test_parse_event_extracts_sorted_buckets_and_meta():
    market = _load_market()
    assert market.target_date == TARGET
    assert market.url.endswith("/event/highest-temperature-in-los-angeles-on-july-16-2026")
    assert round(market.volume_usd) == 38402
    # Диапазоны по возрастанию: открытый вниз — первым, открытый вверх — последним.
    assert market.buckets[0].label == "77°F or below"
    assert market.buckets[-1].label == "96°F or higher"
    assert [b.prob for b in market.buckets[:3]] == [0.026, 0.185, 0.495]


def test_parse_event_empty_payload_means_market_not_created():
    assert polymarket.parse_event([], TARGET) is None


def test_parse_event_skips_unknown_bucket_but_keeps_rest():
    payload = [{
        "slug": "s",
        "volume": 1.0,
        "markets": [
            {"groupItemTitle": "чепуха", "outcomes": '["Yes","No"]',
             "outcomePrices": '["0.5","0.5"]'},
            {"groupItemTitle": "80-81°F", "outcomes": '["Yes","No"]',
             "outcomePrices": '["0.4","0.6"]'},
        ],
    }]
    market = polymarket.parse_event(payload, TARGET)
    assert [b.label for b in market.buckets] == ["80-81°F"]


def test_parse_event_raises_when_no_bucket_recognized():
    payload = [{"slug": "s", "markets": [{"groupItemTitle": "чепуха"}]}]
    with pytest.raises(ParseError):
        polymarket.parse_event(payload, TARGET)


def test_yes_price_found_by_outcome_name_not_position():
    payload = [{
        "slug": "s",
        "volume": 0,
        "markets": [{
            "groupItemTitle": "80-81°F",
            "outcomes": '["No","Yes"]',          # обратный порядок исходов
            "outcomePrices": '["0.6","0.4"]',
        }],
    }]
    market = polymarket.parse_event(payload, TARGET)
    assert market.buckets[0].prob == 0.4


# --- Выбор диапазона для прогноза ---------------------------------------------

def test_bucket_for_covers_open_and_closed_ranges():
    market = _load_market()
    assert market.bucket_for(60.0).label == "77°F or below"   # открыт вниз
    assert market.bucket_for(81.0).label == "80-81°F"
    assert market.bucket_for(120.0).label == "96°F or higher"  # открыт вверх


def test_bucket_for_rounds_to_whole_degrees_like_market():
    market = _load_market()
    # Рынок резолвится в целых °F — прогноз сопоставляется так же.
    assert market.bucket_for(79.6).label == "80-81°F"


# --- Форматирование блока в /forecast -------------------------------------------

def _fp(model: str, tmax: float) -> ForecastPoint:
    return ForecastPoint(
        target_date=TARGET, model=model,
        cycle=datetime(2026, 7, 15, 12, tzinfo=timezone.utc), tmax_f=tmax,
    )


def test_format_market_marks_models_and_links():
    market = _load_market()
    out = formatting.format_market(
        market, {"NBM": _fp("NBM", 81.0), "MAV": _fp("MAV", 80.0),
                 "MET": _fp("MET", 83.0)}
    )
    assert "Polymarket" in out and "KLAX" in out
    assert "80-81°F" in out and "50%" in out
    assert "← NBM, MAV" in out          # оба прогноза в одном диапазоне
    assert "← MET" in out
    assert 'href="https://polymarket.com/event/' in out
    assert "Объём $38,402" in out
    # Хвост с ничтожной вероятностью и без прогнозов моделей скрыт.
    assert "96°F or higher" not in out


def test_format_market_shows_tiny_bucket_when_model_lands_in_it():
    market = _load_market()
    out = formatting.format_market(market, {"MET": _fp("MET", 89.0)})
    # 88-89°F имеет prob 0.25% (<1%), но показывается из-за прогноза MET.
    assert "88-89°F" in out
    assert "&lt;1%" in out  # '<' экранирован для parse_mode=HTML


def test_format_market_none_is_honest():
    out = formatting.format_market(None, {"NBM": None})
    assert "Polymarket" in out
    assert "недоступен" in out
