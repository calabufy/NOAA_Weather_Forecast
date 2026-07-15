# nws.py — доступ к api.weather.gov.
# Point forecast (/gridpoints/LOX/...) как опциональный третий прогноз и
# наблюдения METAR (/stations/KLAX/observations) как fallback-источник факта
# для верификации, когда CLI-отчёт ещё не опубликован.
#
# На Фазе 1 реализован METAR-путь: наблюдения за окно суток -> фактический Tmax
# (max по часовым температурам, °C -> °F). Point forecast отложен.

from __future__ import annotations

from datetime import date, datetime

from app import config, timeutil
from app.sources import (
    ActualTmax,
    Observation,
    ParseError,
    c_to_f,
    http_get_json,
)

SOURCE = "METAR"


def parse_observations(payload: dict) -> list[Observation]:
    """Разобрать ответ api.weather.gov/observations в список наблюдений (°F).

    Пропускает наблюдения без температуры (temperature.value == null).
    """
    feats = payload.get("features")
    if feats is None:
        raise ParseError("в ответе METAR нет поля 'features'")
    out: list[Observation] = []
    for f in feats:
        p = f.get("properties", {})
        temp = (p.get("temperature") or {}).get("value")
        ts = p.get("timestamp")
        if temp is None or ts is None:
            continue
        dt = datetime.fromisoformat(ts).astimezone(timeutil.UTC)
        out.append(Observation(ts_utc=dt, temp_f=c_to_f(float(temp))))
    return out


def max_tmax_for_day(obs: list[Observation], d: date) -> ActualTmax | None:
    """Фактический Tmax локальных суток d как max наблюдений в их границах.

    Возвращает None, если в окне суток нет ни одного наблюдения.
    """
    start, end = timeutil.local_day_bounds(d)
    day_temps = [o.temp_f for o in obs if start <= o.ts_utc < end]
    if not day_temps:
        return None
    return ActualTmax(date=d, tmax_f=max(day_temps), source=SOURCE)


def fetch_observations(start: datetime, end: datetime) -> list[Observation]:
    """Скачать наблюдения METAR станции за окно [start, end)."""
    url = (
        f"{config.METAR_URL_TEMPLATE}"
        f"?start={start.astimezone(timeutil.UTC):%Y-%m-%dT%H:%M:%SZ}"
        f"&end={end.astimezone(timeutil.UTC):%Y-%m-%dT%H:%M:%SZ}"
    )
    return parse_observations(http_get_json(url))


def fetch_actual(d: date) -> ActualTmax | None:
    """Полный путь: скачать наблюдения за сутки d и вернуть фактический Tmax."""
    start, end = timeutil.local_day_bounds(d)
    return max_tmax_for_day(fetch_observations(start, end), d)