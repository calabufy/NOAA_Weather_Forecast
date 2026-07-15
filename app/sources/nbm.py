# nbm.py — источник прогноза NBM (National Blend of Models), станционный бюллетень NBS.
# Скачивает текстовый бюллетень для KLAX и парсит прогноз Tmax на target-дату.
# Основная модель прогноза. Возвращает чистые данные без записи в БД.
#
# NBS публикуется единым bulk-файлом со всеми станциями (~28 МБ) за дату/цикл;
# нужный блок KLAX вырезаем стримингом, не загружая файл целиком.

from __future__ import annotations

from datetime import date
from typing import Iterable

from app import config
from app.sources import (
    ForecastPoint,
    ParseError,
    http_stream_extract,
    parse_fixed_bulletin,
)

MODEL = "NBM"
_STATION_PREFIX = f" {config.STATION} "  # блок станции начинается с ' KLAX '


def extract_station_block(lines: Iterable[str]) -> str:
    """Вырезать блок нужной станции из потока строк bulk-файла NBS."""
    block: list[str] = []
    collecting = False
    for ln in lines:
        if ln.startswith(_STATION_PREFIX):
            collecting = True
        elif collecting and not ln.strip():
            break  # пустая строка — конец блока станции
        if collecting:
            block.append(ln)
    if not block:
        raise ParseError(f"блок станции {config.STATION} не найден в NBS")
    return "\n".join(block)


def fetch_nbs_raw(run_date: date, cycle: str) -> str:
    """Скачать и вернуть сырой текстовый блок KLAX из NBS за дату/цикл."""
    url = config.NBS_URL_TEMPLATE.format(
        date=run_date.strftime("%Y%m%d"), cycle=cycle
    )
    return http_stream_extract(url, extract_station_block)


def parse_nbs(text: str) -> list[ForecastPoint]:
    """Разобрать блок NBS в прогнозы Tmax по локальным суткам LA.

    Строка часов — 'UTC', строка max/min — 'TXN'.
    """
    return parse_fixed_bulletin(text, MODEL, hours_label="UTC",
                                maxmin_label="TXN")


def fetch_forecast(run_date: date, cycle: str) -> list[ForecastPoint]:
    """Полный путь: скачать блок KLAX и разобрать в прогнозы."""
    return parse_nbs(fetch_nbs_raw(run_date, cycle))
