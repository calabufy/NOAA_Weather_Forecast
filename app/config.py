# config.py — конфигурация приложения.
# Читает переменные окружения из .env (BOT_TOKEN, ALLOWED_CHAT_IDS, DB_PATH)
# и хранит константы: код станции (KLAX), таймзона (America/Los_Angeles),
# URL источников данных NOAA (NBM, MAV, CLI, METAR). Единая точка настроек.
#
# На Фазе 1 задействована только часть, нужная источникам и модулю времени:
# станция, таймзона, User-Agent и URL-шаблоны. Секреты бота/БД подключатся позже.

from __future__ import annotations

import os

# --- Станция и география ---------------------------------------------------
STATION = "KLAX"            # ICAO-код станции (Los Angeles Intl Airport)
WFO = "LOX"                 # офис NWS (Los Angeles/Oxnard)
CLI_OFFICE = "KLOX"         # issuingOffice для CLI-продуктов в api.weather.gov
CLI_STATION_TITLE = "LOS ANGELES INTL AIRPORT"  # маркер нужного CLI среди станций офиса
CLI_AWIPS_ID = "CLILAX"     # AWIPS-идентификатор нужного CLI-бюллетеня

TZ = "America/Los_Angeles"  # климатические сутки станции считаются в этой зоне

# --- HTTP ------------------------------------------------------------------
# api.weather.gov и серверы NOAA требуют осмысленный User-Agent с контактом,
# иначе отвечают 403. Значение берётся из окружения, дефолт — с контактом проекта.
HTTP_CONTACT = os.getenv("HTTP_CONTACT", "qoloortea@gmail.com")
HTTP_USER_AGENT = os.getenv(
    "HTTP_USER_AGENT", f"LA-Weather-Forecast/0.1 ({HTTP_CONTACT})"
)
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "60"))

# --- URL-шаблоны источников (конфигурируемые: пути NOAA периодически меняются) --
# NBM / NBS: единый bulk-файл со всеми станциями за дату/цикл (~28 МБ, стримим).
#   {date}=YYYYMMDD, {cycle}=00|06|12|18
NBS_URL_TEMPLATE = os.getenv(
    "NBS_URL_TEMPLATE",
    "https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod/"
    "blend.{date}/{cycle}/text/blend_nbstx.t{cycle}z",
)

# GFS MOS / MAV: коллективный текстовый файл MDL по циклу (все станции).
#   {cycle}=00|06|12|18
MAV_URL_TEMPLATE = os.getenv(
    "MAV_URL_TEMPLATE",
    "https://www.weather.gov/source/mdl/MOS/GFSMAV.t{cycle}z",
)

# CLI: список продуктов офиса и получение текста конкретного продукта.
CLI_LIST_URL = os.getenv(
    "CLI_LIST_URL",
    f"https://api.weather.gov/products?type=CLI&office={CLI_OFFICE}&limit=25",
)
CLI_PRODUCT_URL = os.getenv(
    "CLI_PRODUCT_URL", "https://api.weather.gov/products/{product_id}"
)

# METAR: наблюдения станции за окно [start, end).
METAR_URL_TEMPLATE = os.getenv(
    "METAR_URL_TEMPLATE",
    "https://api.weather.gov/stations/" + STATION + "/observations",
)

# Доступные циклы моделей (UTC-часы выпуска).
MODEL_CYCLES = ("00", "06", "12", "18")