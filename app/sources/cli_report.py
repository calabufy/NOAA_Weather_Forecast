# cli_report.py — парсинг официального NWS CLI-отчёта (CLILAX) офиса LOX.
# Извлекает канонический фактический Tmax за прошедшие сутки — основной источник
# факта для верификации прогнозов.
#
# Офис KLOX выпускает CLI отдельными продуктами по многим станциям; нужный —
# с AWIPS-идентификатором 'CLILAX' (Los Angeles Intl Airport). Текст доступен
# через api.weather.gov (поле productText).

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import date

from app import config
from app.sources import ActualTmax, ParseError, http_get_json

SOURCE = "CLI"
log = logging.getLogger(__name__)
_PRODUCT_HEADER_LENGTH = 400

_MONTHS = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "MAY": 5, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11,
    "DECEMBER": 12,
}

# «...CLIMATE SUMMARY FOR JULY 10 2026...» — сутки, к которым относится MAXIMUM.
_SUMMARY_RE = re.compile(
    r"CLIMATE SUMMARY FOR\s+([A-Z]+)\s+(\d{1,2})\s+(\d{4})"
)
# Строка «  MAXIMUM         74   2:44 PM ...» — берём первое число (наблюдённый Tmax).
_MAXIMUM_RE = re.compile(r"^\s*MAXIMUM\s+(-?\d+)", re.MULTILINE)


def _is_lax(product_text: str) -> bool:
    """Это ли CLI именно для KLAX (по AWIPS-id / заголовку станции)."""
    head = product_text[:_PRODUCT_HEADER_LENGTH].upper()
    return config.CLI_AWIPS_ID in head or config.CLI_STATION_TITLE in head


def iter_lax_products(limit: int = 25) -> Iterator[str]:
    """Перебрать тексты CLILAX от новых к старым, пропуская чужие станции."""
    url = (
        config.CLI_LIST_URL
        if limit == 25
        else config.CLI_LIST_URL_TEMPLATE.format(
            office=config.CLI_OFFICE,
            location=config.CLI_LOCATION,
            limit=limit,
        )
    )
    listing = http_get_json(url)
    products = listing.get("@graph", [])
    for item in products:  # список отсортирован от свежих к старым
        product_id = item["@id"].rsplit("/", 1)[-1]
        try:
            obj = http_get_json(
                config.CLI_PRODUCT_URL.format(product_id=product_id)
            )
        except Exception:  # noqa: BLE001 — один продукт не прерывает весь архив
            log.warning("не удалось получить CLI-продукт %s", product_id)
            continue
        text = obj.get("productText", "")
        if _is_lax(text):
            yield text


def fetch_latest_cli() -> str:
    """Вернуть productText свежего CLILAX (обходя другие станции офиса)."""
    text = next(iter_lax_products(), None)
    if text is not None:
        return text
    raise ParseError("свежий CLILAX не найден среди продуктов офиса")


def parse_cli_tmax(text: str) -> ActualTmax:
    """Извлечь дату суток и фактический Tmax из текста CLI-отчёта."""
    m_date = _SUMMARY_RE.search(text)
    if m_date is None:
        raise ParseError("не найдена строка 'CLIMATE SUMMARY FOR ...'")
    month = _MONTHS.get(m_date.group(1))
    if month is None:
        raise ParseError(f"неизвестный месяц: {m_date.group(1)}")
    d = date(int(m_date.group(3)), month, int(m_date.group(2)))

    m_max = _MAXIMUM_RE.search(text)
    if m_max is None:
        raise ParseError("не найдена строка MAXIMUM с фактическим Tmax")
    return ActualTmax(date=d, tmax_f=float(int(m_max.group(1))), source=SOURCE)


def fetch_actual() -> ActualTmax:
    """Полный путь: получить свежий CLILAX и разобрать фактический Tmax."""
    return parse_cli_tmax(fetch_latest_cli())
