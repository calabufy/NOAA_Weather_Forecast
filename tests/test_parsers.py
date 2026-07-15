# test_parsers.py — тесты парсеров источников (NBM/MAV/CLI/METAR) на образцах из
# fixtures/, включая проверку устойчивости к неожиданному/«сломанному» формату.

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.sources import ParseError, cli_report, mav, met, nbm, nws

FIXTURES = Path(__file__).parent / "fixtures"
UTC = timezone.utc


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


def _read_json(name: str):
    return json.loads(_read(name))


# --- NBS / NBM -------------------------------------------------------------

def test_parse_nbs_extracts_daily_maxes():
    points = nbm.parse_nbs(_read("nbs_klax.txt"))
    by_date = {p.target_date: p.tmax_f for p in points}
    # Цикл 12Z 11 июля: дневные максимумы TXN = 75 на 11–13 июля.
    assert by_date[date(2026, 7, 11)] == 75.0
    assert by_date[date(2026, 7, 12)] == 75.0
    assert all(p.model == "NBM" for p in points)
    assert all(p.cycle == datetime(2026, 7, 11, 12, tzinfo=UTC) for p in points)
    # Неполный последний день (только утро) не попадает в результат.
    assert date(2026, 7, 14) not in by_date


def test_parse_mav_extracts_daily_maxes():
    points = mav.parse_mav(_read("mav_klax.txt"))
    by_date = {p.target_date: p.tmax_f for p in points}
    # Цикл 12Z 10 июля: N/X дневной максимум на 11 июля = 74.
    assert by_date[date(2026, 7, 11)] == 74.0
    assert all(p.model == "MAV" for p in points)
    # Неполные краевые сутки (утро выпуска, последнее утро) отброшены.
    assert date(2026, 7, 10) not in by_date
    assert date(2026, 7, 13) not in by_date


def test_parse_met_extracts_daily_maxes():
    points = met.parse_met(_read("met_klax.txt"))
    by_date = {p.target_date: p.tmax_f for p in points}
    # Цикл 00Z 14 июля, NAM MOS: дневные максимумы X/N = 78 на 15 июля, 77 на 16-е.
    assert by_date[date(2026, 7, 15)] == 78.0
    assert by_date[date(2026, 7, 16)] == 77.0
    assert all(p.model == "MET" for p in points)
    assert all(p.cycle == datetime(2026, 7, 14, 0, tzinfo=UTC) for p in points)


def test_met_cycle_maps_to_available_00_12():
    # NAM MOS есть только на 00Z/12Z: 06->00, 18->12 (иначе 404).
    assert [met.mos_cycle(c) for c in ("00", "06", "12", "18")] == ["00", "00", "12", "12"]


def test_bulletin_parser_raises_on_garbage():
    with pytest.raises(ParseError):
        nbm.parse_nbs("совершенно не тот формат\nбез шапки и колонок\n")


# --- CLI -------------------------------------------------------------------

def test_parse_cli_tmax():
    text = _read_json("cli_lax.json")["productText"]
    actual = cli_report.parse_cli_tmax(text)
    assert actual.date == date(2026, 7, 10)
    assert actual.tmax_f == 74.0
    assert actual.source == "CLI"


def test_parse_cli_raises_without_maximum():
    with pytest.raises(ParseError):
        cli_report.parse_cli_tmax("CLIMATE SUMMARY FOR JULY 10 2026\n(нет строки MAXIMUM)")


# --- METAR -----------------------------------------------------------------

def test_metar_max_for_day_matches_cli():
    obs = nws.parse_observations(_read_json("metar_klax.json"))
    assert len(obs) > 100
    actual = nws.max_tmax_for_day(obs, date(2026, 7, 10))
    assert actual is not None
    assert actual.source == "METAR"
    # METAR-максимум близок к каноничному CLI (74°F): расхождение < 1°F.
    assert abs(actual.tmax_f - 74.0) < 1.0


def test_metar_max_for_day_none_when_no_obs():
    obs = nws.parse_observations(_read_json("metar_klax.json"))
    # Сутки без наблюдений в окне -> None.
    assert nws.max_tmax_for_day(obs, date(2020, 1, 1)) is None


def test_metar_c_to_f_conversion():
    payload = {"features": [{"properties": {
        "timestamp": "2026-07-10T20:00:00+00:00",
        "temperature": {"value": 20.0},
    }}]}
    obs = nws.parse_observations(payload)
    assert obs[0].temp_f == pytest.approx(68.0)  # 20°C = 68°F


def test_metar_skips_null_temperature():
    payload = {"features": [{"properties": {
        "timestamp": "2026-07-10T20:00:00+00:00",
        "temperature": {"value": None},
    }}]}
    assert nws.parse_observations(payload) == []