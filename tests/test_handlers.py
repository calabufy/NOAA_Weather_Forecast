import asyncio
from datetime import date, datetime, timezone

from app.bot import handlers
from app.sources import ForecastPoint


class Message:
    def __init__(self):
        self.answers = []
        self.photos = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)

    async def answer_photo(self, photo, caption=None, **kwargs):
        self.photos.append((photo, caption))


class FakeConn:
    def close(self):
        pass


def test_forecast_db_failure_returns_message(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(handlers.repo, "connect", fail)
    message = Message()

    asyncio.run(handlers.cmd_forecast(message))

    assert message.answers == ["Не удалось получить прогноз, попробуйте позже."]


def test_forecast_empty_db_returns_not_collected(monkeypatch):
    monkeypatch.setattr(handlers.repo, "connect", lambda: FakeConn())
    monkeypatch.setattr(handlers.repo, "latest_forecast", lambda conn, d, m: None)
    message = Message()

    asyncio.run(handlers.cmd_forecast(message))

    assert message.answers == ["Прогноз на завтра ещё не собран, попробуйте позже."]


def _seed_forecast_db(monkeypatch, target):
    monkeypatch.setattr(handlers.timeutil, "la_tomorrow", lambda: target)
    monkeypatch.setattr(handlers.repo, "connect", lambda: FakeConn())

    def fake_latest(conn, d, model):
        assert d == target
        if model == "NBM":
            return ForecastPoint(
                target_date=d, model=model,
                cycle=datetime(2026, 7, 16, 12, tzinfo=timezone.utc), tmax_f=81.0,
            )
        return None  # у остальных моделей прогноза ещё нет

    monkeypatch.setattr(handlers.repo, "latest_forecast", fake_latest)


def test_forecast_renders_points_from_db(monkeypatch):
    target = date(2026, 7, 17)
    _seed_forecast_db(monkeypatch, target)
    # Рынка на эту дату нет — прогноз уходит без блока Polymarket.
    monkeypatch.setattr(handlers.polymarket, "fetch_market", lambda d: None)
    message = Message()

    asyncio.run(handlers.cmd_forecast(message))

    assert len(message.answers) == 1
    text = message.answers[0]
    assert "NBM" in text and "81" in text
    assert "прогноз ещё не собран" in text  # MAV/MET без данных помечены честно
    assert "Polymarket" not in text


def test_forecast_appends_market_block(monkeypatch):
    target = date(2026, 7, 17)
    _seed_forecast_db(monkeypatch, target)
    market = handlers.polymarket.TempMarket(
        target_date=target,
        title="Highest temperature in Los Angeles on July 17?",
        url="https://polymarket.com/event/test-slug",
        volume=12345.0,
        buckets=[
            handlers.polymarket.TempBucket("80-81°F", 80, 81, 0.4),
            handlers.polymarket.TempBucket("82°F or higher", 82, None, 0.6),
        ],
    )
    monkeypatch.setattr(handlers.polymarket, "fetch_market", lambda d: market)
    message = Message()

    asyncio.run(handlers.cmd_forecast(message))

    text = message.answers[0]
    assert "Polymarket" in text
    assert "← NBM" in text  # 81.0 попадает в корзину 80-81
    assert "https://polymarket.com/event/test-slug" in text


def test_forecast_survives_market_failure(monkeypatch):
    target = date(2026, 7, 17)
    _seed_forecast_db(monkeypatch, target)

    def boom(d):
        raise RuntimeError("polymarket down")

    monkeypatch.setattr(handlers.polymarket, "fetch_market", boom)
    message = Message()

    asyncio.run(handlers.cmd_forecast(message))

    text = message.answers[0]
    assert "NBM" in text and "Polymarket" not in text


def test_chart_sends_png_from_metric_reports(monkeypatch):
    target = date(2026, 7, 17)
    monkeypatch.setattr(handlers.timeutil, "la_today", lambda: target)
    monkeypatch.setattr(handlers.repo, "connect", lambda: FakeConn())
    monkeypatch.setattr(
        handlers.repo,
        "daily_error_series",
        lambda conn, model, start, end: [(date(2026, 7, 16), 82.0, 80.0)],
    )
    message = Message()

    asyncio.run(handlers.cmd_chart(message))

    assert message.answers == []
    assert len(message.photos) == 1
    photo, caption = message.photos[0]
    assert photo.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert photo.filename == "klax-metrics-2026-07-16.png"
    assert "/errors" in caption


def test_chart_without_data_returns_message(monkeypatch):
    monkeypatch.setattr(handlers.repo, "connect", lambda: FakeConn())
    monkeypatch.setattr(handlers.repo, "daily_error_series", lambda *args: [])
    message = Message()

    asyncio.run(handlers.cmd_chart(message))

    assert message.answers == ["Для графика пока недостаточно данных."]
    assert message.photos == []
