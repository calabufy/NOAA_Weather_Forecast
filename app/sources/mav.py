# mav.py — источник прогноза GFS MOS (бюллетень MAV) для KLAX.
# Скачивает и парсит текстовый MOS-бюллетень фиксированного формата, извлекает Tmax.
# Второй прогноз — для сравнения качества с NBM.
#
# MAV публикуется коллективным файлом MDL со всеми станциями за цикл; нужный
# блок KLAX вырезаем стримингом.

from __future__ import annotations

from datetime import date

from app import config
from app.sources import (
    ForecastPoint,
    http_stream_extract,
    parse_fixed_bulletin,
)
from app.sources.nbm import extract_station_block

MODEL = "MAV"


def fetch_mav_raw(cycle: str) -> str:
    """Скачать и вернуть сырой текстовый блок KLAX из коллективного MAV за цикл."""
    url = config.MAV_URL_TEMPLATE.format(cycle=cycle)
    return http_stream_extract(url, extract_station_block)


def parse_mav(text: str) -> list[ForecastPoint]:
    """Разобрать блок MAV в прогнозы Tmax по локальным суткам LA.

    Строка часов — 'HR', строка max/min — 'N/X'.
    """
    return parse_fixed_bulletin(text, MODEL, hours_label="HR",
                                maxmin_label="N/X")


def fetch_forecast(_run_date: date, cycle: str) -> list[ForecastPoint]:
    """Полный путь: скачать блок KLAX и разобрать в прогнозы."""
    return parse_mav(fetch_mav_raw(cycle))
