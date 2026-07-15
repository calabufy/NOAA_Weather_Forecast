# met.py — источник прогноза NAM MOS (бюллетень MET) для KLAX.
# Скачивает и парсит текстовый MOS-бюллетень фиксированного формата, извлекает Tmax.
# Третий прогноз — независимая модель (NAM, а не GFS): даёт «второе мнение» рядом
# с NBM/MAV при их расхождении.
#
# MET публикуется коллективным файлом MDL со всеми станциями за цикл; нужный
# блок KLAX вырезаем стримингом (как MAV). Формат тот же MOS, но строка максимума
# помечена 'X/N' (у GFS MOS — 'N/X').
#
# ВАЖНО: NAM MOS выходит только из циклов 00Z и 12Z (06/18 отдают 404). Поэтому
# запрошенный общий цикл приводим к последнему доступному 00/12 — иначе на 06/18
# получали бы 404 (а в коллекторе это ERROR → ложный алерт). Итоговый цикл прогноза
# берётся из шапки бюллетеня самим парсером, так что подмена не искажает данные.

from __future__ import annotations

from datetime import date

from app import config
from app.sources import (
    ForecastPoint,
    http_stream_extract,
    parse_fixed_bulletin,
)
from app.sources.nbm import extract_station_block

MODEL = "MET"


def mos_cycle(cycle: str) -> str:
    """Последний доступный цикл NAM MOS (00 или 12) для запрошенного 00/06/12/18."""
    return f"{(int(cycle) // 12) * 12:02d}"


def fetch_met_raw(cycle: str) -> str:
    """Скачать и вернуть сырой текстовый блок KLAX из коллективного MET за цикл."""
    url = config.MET_URL_TEMPLATE.format(cycle=mos_cycle(cycle))
    return http_stream_extract(url, extract_station_block)


def parse_met(text: str) -> list[ForecastPoint]:
    """Разобрать блок MET в прогнозы Tmax по локальным суткам LA.

    Строка часов — 'HR', строка max/min — 'X/N'.
    """
    return parse_fixed_bulletin(text, MODEL, hours_label="HR",
                                maxmin_label="X/N")


def fetch_forecast(_run_date: date, cycle: str) -> list[ForecastPoint]:
    """Полный путь: скачать блок KLAX и разобрать в прогнозы."""
    return parse_met(fetch_met_raw(cycle))
