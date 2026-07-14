# test_live.py — тесты ленивого забора прогноза для /forecast (app/bot/live.py).
# Проверяем: выбор прогноза на завтра из свежего цикла, изоляцию сбоя модели,
# кэширование по циклу (повтор не качает) и сброс кэша при смене цикла.
# Сеть не трогаем — источники и latest_cycle подменяются monkeypatch.

import asyncio
from datetime import date, datetime, timezone

from app import config
from app.bot import live
from app.sources import ForecastPoint

UTC = timezone.utc
CYCLE = datetime(2026, 7, 14, 18, tzinfo=UTC)
TOMORROW = date(2026, 7, 15)


def _fp(model: str, target: date, tmax: float) -> ForecastPoint:
    return ForecastPoint(target_date=target, model=model, cycle=CYCLE, tmax_f=tmax)


def _setup(monkeypatch, *, run_date=date(2026, 7, 14), cycle="18",
           tomorrow=TOMORROW):
    """Общая подготовка: фиксируем цикл/завтра и чистим кэш забора."""
    live._cache.clear()
    monkeypatch.setattr(live, "latest_cycle", lambda: (run_date, cycle))
    monkeypatch.setattr(live.timeutil, "la_tomorrow", lambda: tomorrow)


def test_picks_tomorrow_from_fresh_cycle(monkeypatch):
    _setup(monkeypatch)
    # В бюллетене несколько дней — берём именно завтрашний.
    monkeypatch.setattr(live.nbm, "fetch_forecast", lambda d, c: [
        _fp("NBM", date(2026, 7, 14), 77.0),
        _fp("NBM", TOMORROW, 81.0),
    ])
    monkeypatch.setattr(live.mav, "fetch_forecast", lambda c: [
        _fp("MAV", TOMORROW, 78.0),
    ])
    target, points = asyncio.run(live.forecast_tomorrow())
    assert target == TOMORROW
    assert points["NBM"].tmax_f == 81.0
    assert points["MAV"].tmax_f == 78.0


def test_model_failure_isolated(monkeypatch):
    _setup(monkeypatch)

    def boom(*_a):
        raise RuntimeError("сеть легла")

    monkeypatch.setattr(live.nbm, "fetch_forecast", boom)
    monkeypatch.setattr(live.mav, "fetch_forecast", lambda c: [_fp("MAV", TOMORROW, 78.0)])
    _target, points = asyncio.run(live.forecast_tomorrow())
    assert points["NBM"] is None            # упавшая модель -> «—»
    assert points["MAV"].tmax_f == 78.0     # вторая отработала


def test_missing_tomorrow_gives_none(monkeypatch):
    _setup(monkeypatch)
    # Свежий цикл есть, но колонки на завтра в нём нет.
    monkeypatch.setattr(live.nbm, "fetch_forecast",
                        lambda d, c: [_fp("NBM", date(2026, 7, 14), 77.0)])
    monkeypatch.setattr(live.mav, "fetch_forecast", lambda c: [])
    _target, points = asyncio.run(live.forecast_tomorrow())
    assert points["NBM"] is None
    assert points["MAV"] is None


def test_cache_hit_skips_refetch(monkeypatch):
    _setup(monkeypatch)
    calls = {"nbm": 0, "mav": 0}

    def nbm_fetch(d, c):
        calls["nbm"] += 1
        return [_fp("NBM", TOMORROW, 81.0)]

    def mav_fetch(c):
        calls["mav"] += 1
        return [_fp("MAV", TOMORROW, 78.0)]

    monkeypatch.setattr(live.nbm, "fetch_forecast", nbm_fetch)
    monkeypatch.setattr(live.mav, "fetch_forecast", mav_fetch)

    asyncio.run(live.forecast_tomorrow())
    asyncio.run(live.forecast_tomorrow())  # тот же цикл в пределах TTL — из кэша
    assert calls == {"nbm": 1, "mav": 1}


def test_new_cycle_busts_cache(monkeypatch):
    _setup(monkeypatch)
    calls = {"n": 0}

    def nbm_fetch(d, c):
        calls["n"] += 1
        return [_fp("NBM", TOMORROW, 81.0)]

    monkeypatch.setattr(live.nbm, "fetch_forecast", nbm_fetch)
    monkeypatch.setattr(live.mav, "fetch_forecast", lambda c: [])

    asyncio.run(live.forecast_tomorrow())
    monkeypatch.setattr(live, "latest_cycle", lambda: (date(2026, 7, 15), "00"))
    asyncio.run(live.forecast_tomorrow())   # новый цикл -> кэш промахивается
    assert calls["n"] == 2


def test_total_failure_not_cached(monkeypatch):
    _setup(monkeypatch)
    calls = {"n": 0}

    def boom(*_a):
        calls["n"] += 1
        raise RuntimeError("нет сети")

    monkeypatch.setattr(live.nbm, "fetch_forecast", boom)
    monkeypatch.setattr(live.mav, "fetch_forecast", boom)

    asyncio.run(live.forecast_tomorrow())
    asyncio.run(live.forecast_tomorrow())   # полный провал не кэшируется — пробуем снова
    assert calls["n"] == 4                   # 2 модели × 2 запроса
    assert live._cache == {}
