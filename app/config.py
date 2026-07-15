# config.py — конфигурация приложения.
# Читает переменные окружения из .env (BOT_TOKEN, ALLOWED_CHAT_IDS, DB_PATH)
# и хранит константы: код станции (KLAX), таймзона (America/Los_Angeles),
# URL источников данных NOAA (NBM, MAV, CLI, METAR). Единая точка настроек.
#
# На Фазе 1 задействована часть, нужная источникам и модулю времени. Фаза 2
# добавляет путь к БД и расписание джобов. Секреты бота подключатся на Фазе 4.

from __future__ import annotations

import os
from pathlib import Path

# Корень проекта (родитель пакета app/) — якорь для относительных путей, чтобы
# они не зависели от рабочей директории процесса.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Подхватываем .env в os.environ до чтения переменных (локальный запуск вне
# Docker: python -m app.main). В Docker/compose переменные передаются окружением,
# .env-файла в образе может не быть — поэтому загрузка «best-effort», а сам
# python-dotenv опционален (без него модуль всё равно импортируется, напр. в CI).
try:
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:
    pass

# --- Станция и география ---------------------------------------------------
STATION = "KLAX"            # ICAO-код станции (Los Angeles Intl Airport)
WFO = "LOX"                 # офис NWS (Los Angeles/Oxnard)
CLI_OFFICE = "KLOX"         # issuingOffice для CLI-продуктов в api.weather.gov
CLI_STATION_TITLE = "LOS ANGELES INTL AIRPORT"  # маркер нужного CLI среди станций офиса
CLI_AWIPS_ID = "CLILAX"     # AWIPS-идентификатор нужного CLI-бюллетеня
CLI_LOCATION = "LAX"        # locationId NWS Products API для CLILAX

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

# NAM MOS / MET: коллективный текстовый файл MDL по циклу (все станции).
# Независимая от GFS модель (NAM); формат MOS тот же, что у MAV. {cycle}=00|06|12|18
MET_URL_TEMPLATE = os.getenv(
    "MET_URL_TEMPLATE",
    "https://www.weather.gov/source/mdl/MOS/NAMMET.t{cycle}z",
)

# CLI: список продуктов офиса и получение текста конкретного продукта.
CLI_LIST_URL_TEMPLATE = os.getenv(
    "CLI_LIST_URL_TEMPLATE",
    "https://api.weather.gov/products?type=CLI&office={office}"
    "&location={location}&limit={limit}",
)
# Старое переопределение сохраняется для обычного запроса свежих 25 продуктов.
CLI_LIST_URL = os.getenv(
    "CLI_LIST_URL",
    CLI_LIST_URL_TEMPLATE.format(
        office=CLI_OFFICE, location=CLI_LOCATION, limit=25
    ),
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

# --- Polymarket (данные рынка ставок на Tmax LA) -----------------------------
# Ежедневное событие «Highest temperature in Los Angeles on <date>?» резолвится
# по той же станции KLAX (Wunderground, целые °F), что и наши прогнозы/факты.
# Gamma API — публичный read-only без ключа; slug события строится из даты.
POLYMARKET_API_URL_TEMPLATE = os.getenv(
    "POLYMARKET_API_URL_TEMPLATE",
    "https://gamma-api.polymarket.com/events?slug={slug}",
)
POLYMARKET_SLUG_TEMPLATE = os.getenv(
    "POLYMARKET_SLUG_TEMPLATE",
    "highest-temperature-in-los-angeles-on-{month}-{day}-{year}",
)
POLYMARKET_EVENT_URL_TEMPLATE = os.getenv(
    "POLYMARKET_EVENT_URL_TEMPLATE", "https://polymarket.com/event/{slug}"
)

# --- База данных -----------------------------------------------------------
# Путь к файлу SQLite. По умолчанию — в корне проекта; в Docker монтируется на
# volume. Хранит forecasts и actuals (см. app/db/schema.sql).
#
# Относительный путь якорим к корню проекта (не к cwd!): иначе процессы, запущенные
# из разных директорий (бот из app/, скрипты из корня), открывали бы РАЗНЫЕ файлы
# weather.db и «не видели» данные друг друга. ':memory:' и абсолютные пути — как есть.
def _resolve_db_path(raw: str) -> str:
    if raw == ":memory:" or os.path.isabs(raw):
        return raw
    return str(_PROJECT_ROOT / raw)


DB_PATH = _resolve_db_path(os.getenv("DB_PATH", "weather.db"))

# --- Расписание джобов (Фаза 2) --------------------------------------------
# Время в зоне станции (America/Los_Angeles). Сбор прогнозов — после того как
# свежий цикл модели опубликован (лаг ниже); верификация — когда за вчера уже
# вышел CLI (утром), с повторной попыткой днём (перезапишет METAR-фолбэк на CLI).
FETCH_HOURS_LA = tuple(
    int(h) for h in os.getenv("FETCH_HOURS_LA", "3,9,15,21").split(",")
)
VERIFY_HOURS_LA = tuple(
    int(h) for h in os.getenv("VERIFY_HOURS_LA", "9,15").split(",")
)
FETCH_MINUTE = int(os.getenv("FETCH_MINUTE", "30"))
VERIFY_MINUTE = int(os.getenv("VERIFY_MINUTE", "0"))
# Бюллетени NBS/MAV появляются не сразу после часа цикла; при выборе «свежего»
# доступного цикла отступаем на этот лаг (часы).
FETCH_LAG_HOURS = float(os.getenv("FETCH_LAG_HOURS", "4"))


# --- Telegram-бот (Фаза 4) -------------------------------------------------
# Токен от BotFather. Обязателен для запуска сервиса (main.py), но не для
# импорта модулей и тестов — поэтому дефолт пустой, а проверка — в точке входа.
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Allowlist: бот личный, отвечает и шлёт алерты только этим chat_id. Пустой
# список означает «никого» — сервис на старте об этом предупредит.
ALLOWED_CHAT_IDS = frozenset(
    int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x
)

# Модели, показываемые в боте (порядок = порядок вывода; NBM — основной).
BOT_MODELS = ("NBM", "MAV", "MET")

# Кэш ленивого забора /forecast (app/bot/live.py): сколько секунд переиспользовать
# уже скачанный прогноз, чтобы не тянуть бюллетени (NBS ~28 МБ) на каждый запрос.
# Новый цикл сбрасывает кэш независимо от TTL (ключ кэша включает цикл).
FORECAST_CACHE_TTL_SEC = float(os.getenv("FORECAST_CACHE_TTL_SEC", "900"))
