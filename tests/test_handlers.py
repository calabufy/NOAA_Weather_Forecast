import asyncio
from datetime import date, datetime, timezone

from app.bot import handlers
from app.sources import ForecastPoint


class Message:
    def __init__(self):
        self.answers = []

    async def answer(self, text):
        self.answers.append(text)


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


def test_forecast_renders_points_from_db(monkeypatch):
    target = date(2026, 7, 17)
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
    message = Message()

    asyncio.run(handlers.cmd_forecast(message))

    assert len(message.answers) == 1
    text = message.answers[0]
    assert "NBM" in text and "81" in text
    assert "прогноз ещё не собран" in text  # MAV/MET без данных помечены честно
