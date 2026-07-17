# План реализации: сервис прогнозирования максимальной температуры (Tmax) на станции KLAX

## 1. Цель и требования

### Функциональные требования
1. Ежедневно получать прогноз максимальной температуры на **следующий день** для станции **KLAX** (Los Angeles Intl Airport) на основе статистических моделей NOAA.
2. Ежедневно получать **фактическую** максимальную температуру за прошедшие сутки и вычислять ошибку прогноза.
3. Накапливать статистику ошибок и агрегировать её за окна: **7 дней, 30 дней, текущий сезон, текущий год**.
4. Telegram-бот с командами:
   - `/forecast` — прогноз Tmax на завтра;
   - `/errors` — сводка ошибок по всем окнам;
   - `/chart` — PNG-график сравнения метрик моделей.

### Нефункциональные требования
- Один инстанс, нагрузка минимальная (личный бот) — без горизонтального масштабирования.
- Надёжность сбора данных важнее latency: ретраи, идемпотентность, пропущенный день не должен ломать статистику.
- Все «сутки» — в часовом поясе станции: **America/Los_Angeles** (климатические сутки = local midnight → midnight).
- Температура хранится в °F (нативные единицы NOAA для станций США), конвертация в °C — на уровне отображения.

---

## 2. Источники данных NOAA

### 2.1. Прогноз (выбрать основной, остальные — опционально для сравнения)

| Источник | Что это | Доступ | Комментарий |
|---|---|---|---|
| **NBM (National Blend of Models)** | Статистически калиброванный бленд моделей NOAA, станционные бюллетени NBS/NBE | Текстовые бюллетени MDL (weather.gov/mdl, NOMADS) | Рекомендуемый основной: современный преемник MOS |
| **GFS MOS (MAV)** | Классический MOS, короткий срок (6–72 ч), выпуски 4 раза/сутки | Текстовый бюллетень для KLAX (MDL / tgftp NWS) | «Каноничный» статистический продукт; парсинг фиксированного текстового формата |
| **NAM MOS (MET)** | MOS поверх модели NAM, короткий срок; выпуски **только 00Z/12Z** | Текстовый бюллетень NAMMET (MDL) | Независимый от GFS сигнал; тот же формат, что MAV (метка максимума `X/N`) |
| **GFS MOS Extended (MEX)** | MOS на 8 суток | Аналогично MAV | Резерв/сравнение |
| NWS point forecast | api.weather.gov `/gridpoints/LOX/...` | JSON REST API | Не чисто статистическая модель (правится синоптиком), но самый удобный API; полезен как второй прогноз для сравнения |

Точные актуальные URL бюллетеней и структуру ответов зафиксировать на этапе реализации (Фаза 1), т.к. пути на серверах NOAA периодически меняются.

**Решение:** тянуть **NBM, MAV и MET параллельно**, хранить все, в боте показывать основной (NBM) + остальные для сравнения. Это даёт сравнение качества моделей бесплатно.

### 2.2. Факт (верификация)

| Источник | Комментарий |
|---|---|
| **NWS CLI report (CLILAX)** | Официальный ежедневный климатический отчёт офиса LOX по LAX. Канонический Tmax суток. Основной источник. |
| CF6 (monthly preliminary) | Резерв/бэкфилл за месяц |
| METAR/observations api.weather.gov `/stations/KLAX/observations` | Fallback: max по часовым (и 6-часовым группам) наблюдениям; чуть менее точен, но доступен сразу |

**Решение:** основной — CLI-отчёт; fallback — расчёт по METAR, с флагом `source` в БД.

---

## 3. Архитектура

```
                ┌──────────────────────────────────────────┐
                │                Scheduler                 │
                │              (APScheduler)           
                │
                └──────┬───────────────┬───────────────────┘
                       │               │
             ┌─────────▼────────┐ ┌────▼─────────────┐
             │ Forecast Fetcher │ │ Verification Job │
             │  NBM / MAV / NWS │ │  CLI / METAR     │
             └─────────┬────────┘ └────┬─────────────┘
                       │               │
                  ┌────▼───────────────▼────┐
                  │        SQLite DB        │
                  │ forecasts / actuals /   │
                  │ errors (view/агрегаты)  │
                  └────────────┬────────────┘
                               │
                    ┌──────────▼──────────┐
                    │    Telegram Bot     │
                    │ /forecast /errors   │
                    │ /chart              │
                    └─────────────────────┘
```

Всё — один процесс (или два: bot + jobs), один Docker-контейнер, volume под SQLite.

### Компоненты

1. **Forecast Fetcher** — по расписанию (после выхода свежих циклов, например 2 раза в день по 00Z и 12Z циклам) скачивает бюллетени, парсит Tmax на завтра (локальная дата LA), пишет в БД. Идемпотентно: ключ `(target_date, model, cycle)`.
2. **Verification Job** — утром по LA-времени (например 09:00 PT, когда CLI за вчера уже опубликован) забирает фактический Tmax за вчерашние сутки, пишет в `actuals`, при отсутствии CLI — fallback на METAR + повторная попытка позже.
3. **Error Calculator** — не отдельный джоб, а SQL-агрегация на лету при команде `/errors` (данных мало, кэш не нужен). Ошибка дня = `forecast(последний цикл до полуночи target-даты) − actual`.
4. **Telegram Bot** — aiogram 3.x, long polling (не нужен публичный хост/webhook).

---

## 4. Модель данных (SQLite)

```sql
CREATE TABLE forecasts (
    id           INTEGER PRIMARY KEY,
    target_date  TEXT NOT NULL,      -- локальная дата LA, YYYY-MM-DD
    model        TEXT NOT NULL,      -- 'NBM' | 'MAV' | 'MET'
    cycle        TEXT NOT NULL,      -- '2026-07-11T12:00Z' — цикл модели
    tmax_f       REAL NOT NULL,
    fetched_at   TEXT NOT NULL,
    UNIQUE(target_date, model, cycle)
);

CREATE TABLE actuals (
    date     TEXT PRIMARY KEY,       -- локальная дата LA
    tmax_f   REAL NOT NULL,
    source   TEXT NOT NULL,          -- 'CLI' | 'METAR'
    fetched_at TEXT NOT NULL
);

-- «Зачётный» прогноз дня: последний цикл каждой модели,
-- вышедший до 00:00 local target-даты (фиксируем правило заранее!)
```

Правило выбора зачётного прогноза фиксируется один раз и не меняется — иначе статистика ошибок несравнима.

---

## 5. Метрики ошибок (`/errors`)

Для каждого окна — **7д / 30д / сезон / год** (сезоны метеорологические: DJF, MAM, JJA, SON; год — календарный или скользящие 365 дней — выбрать и зафиксировать):

- **MAE** — средняя абсолютная ошибка, °F;
- **ME (bias)** — средняя ошибка со знаком (систематическое завышение/занижение);
- **RMSE**;
- **Hit rate**: доля дней с |err| ≤ 1°F, ≤ 2°F, ≤ 3°F;
- max |err| за окно и его дата;
- N дней с полными данными (прогноз+факт) в окне.

Вывод в боте — компактная моноширинная таблица, отдельно по каждой модели.

---

## 6. Технологический стек

| Слой | Выбор | Обоснование |
|---|---|---|
| Язык | Python 3.12 | экосистема, скорость разработки |
| Bot | aiogram 3.x | асинхронный, long polling |
| HTTP | httpx + tenacity (ретраи) | |
| Планировщик | APScheduler (в том же asyncio-loop) | не нужен cron/celery |
| БД | SQLite (+ sqlite-utils / чистый sql) | один писатель, микрообъёмы |
| Время | zoneinfo (`America/Los_Angeles`) | корректный DST |
| Парсинг MOS/NBM | собственный парсер фиксированных текстовых бюллетеней + юнит-тесты на реальных образцах | форматы стабильные, но требуют аккуратности |
| Деплой | Docker + docker-compose, volume для БД | любой VPS |
| Конфиг | .env (BOT_TOKEN, chat allowlist) | |

**Trade-off:** SQLite вместо Postgres — осознанно: один процесс, десятки строк в день. Если появятся другие станции/пользователи в масштабе — миграция тривиальна.

---

## 7. Telegram-бот

Команды:

```
/forecast  → «Прогноз Tmax на завтра (Sun 12 Jul, KLAX):
              NBM: 84°F (28.9°C), цикл 12Z
              MAV: 85°F (29.4°C), цикл 12Z
              MET: 83°F (28.3°C), цикл 12Z»

/errors    → таблица метрик по окнам 7д/30д/сезон/год для каждой модели

/chart     → PNG-сравнение MAE, RMSE, bias и hit-rate NBM/MAV/MET
```

Дополнительно:
- allowlist chat_id (бот личный);
- честные сообщения об отсутствии данных («прогноз цикла 12Z ещё не вышел», «факт за вчера ещё не опубликован в CLI»);
- `/start` с кратким help.

---

## 8. Обработка ошибок и надёжность

- Ретраи с экспоненциальным бэкоффом на все внешние запросы; при неудаче — повтор джоба через 30–60 мин (несколько попыток в течение дня).
- Верификация: если CLI не найден к вечеру — записать METAR-факт с `source='METAR'`, при появлении CLI позже — перезаписать (CLI приоритетнее).
- Парсер бюллетеней: при неожиданном формате — не падать, логировать сырой текст, слать алерт в тот же телеграм-чат.
- Дни без данных исключаются из агрегатов (N показывается в `/errors`).
- Бэкфилл-скрипт: при старте сервиса можно загрузить факт за прошлые даты из CF6/CLI-архива, чтобы окна 30д/сезон наполнились быстрее (прогнозы задним числом не восстанавливаются — статистика ошибок начнёт накапливаться с запуска).

---

## 9. Структура проекта

```
LA_Weather_Forecast/
├── README.md                  # этот план
├── pyproject.toml             # зависимости и метаданные проекта (uv/pip)
├── .env.example               # шаблон конфига: BOT_TOKEN, ALLOWED_CHAT_IDS, DB_PATH
├── .gitignore                 # .env, *.db, __pycache__ и т.п.
├── Dockerfile
├── docker-compose.yml         # сервис + volume под SQLite
│
├── app/
│   ├── __init__.py
│   ├── main.py                # точка входа: поднимает бота и APScheduler в одном asyncio-loop
│   ├── config.py              # чтение .env, константы (станция KLAX, таймзона, URL источников)
│   ├── timeutil.py            # «климатические сутки» LA: границы суток, target-дата «завтра», DST
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── schema.sql         # CREATE TABLE forecasts / actuals (см. раздел 4)
│   │   └── repo.py            # инициализация БД, идемпотентные upsert'ы, выборки
│   │
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── nbm.py             # скачивание и парсинг бюллетеня NBM (NBS) для KLAX
│   │   ├── mav.py             # скачивание и парсинг бюллетеня GFS MOS (MAV)
│   │   ├── met.py             # скачивание и парсинг бюллетеня NAM MOS (MET, 00/12Z)
│   │   ├── nws.py             # api.weather.gov: point forecast (опц.) и METAR-наблюдения
│   │   └── cli_report.py      # парсинг NWS CLI-отчёта (CLILAX) — фактический Tmax
│   │
│   ├── jobs/
│   │   ├── __init__.py
│   │   ├── fetch_forecasts.py # Forecast Fetcher: циклы 00Z/12Z → forecasts
│   │   ├── verify.py          # Verification Job: CLI → actuals, fallback METAR, перезапись
│   │   └── scheduler.py       # регистрация джобов в APScheduler, ретраи/повторы
│   │
│   ├── metrics.py             # MAE, ME, RMSE, hit rate по окнам 7д/30д/сезон/год
│   │
│   └── bot/
│       ├── __init__.py
│       ├── handlers.py        # /start, /forecast, /errors, /chart
│       ├── charts.py          # PNG-график метрик через Pillow
│       ├── formatting.py      # °F→°C, моноширинные таблицы метрик
│       └── middleware.py      # allowlist chat_id, алерты об ошибках пайплайна в чат
│
├── scripts/
│   └── backfill.py            # разовый бэкфилл фактов из CF6/CLI-архива
│
└── tests/
    ├── fixtures/              # реальные образцы бюллетеней NBM/MAV/CLI
    ├── test_parsers.py        # парсеры на образцах, в т.ч. «сломанный» формат
    ├── test_timeutil.py       # границы суток, DST-переходы
    ├── test_metrics.py        # агрегации на синтетике: пропуски, граница сезона/года
    └── test_repo.py           # идемпотентность записи, правило «зачётного» прогноза
```

Принципы разбиения:
- **`sources/`** — только скачивание и парсинг, без записи в БД: каждый модуль возвращает чистые данные, что упрощает юнит-тесты на образцах.
- **`jobs/`** — оркестрация: source → repo, ретраи, идемпотентность.
- **`metrics.py`** — чистые функции над выборками из БД, считаются на лету при `/errors` (раздел 3).
- **`bot/`** ничего не знает об источниках — только читает БД через `repo` и `metrics`.

---

## 10. План работ по фазам

### Фаза 0 — подготовка (0.5 дня)
- [ ] Создать бота у BotFather, получить токен.
- [ ] Репозиторий, скелет проекта, Docker, CI (lint + tests).

### Фаза 1 — источники данных (1–2 дня)
- [ ] Зафиксировать актуальные URL: NBM/MAV бюллетени для KLAX, CLI-отчёт LOX, METAR API.
- [ ] Скачать образцы бюллетеней, написать парсеры + юнит-тесты на образцах.
- [ ] Модуль «климатические сутки LA» (границы суток, выбор target-даты «завтра»).

### Фаза 2 — хранилище и джобы (1 день)
- [ ] Схема SQLite, миграция.
- [ ] Forecast Fetcher + Verification Job на APScheduler, идемпотентность.
- [ ] Правило «зачётного» прогноза (последний цикл до local midnight).

### Фаза 3 — метрики (0.5 дня)
- [ ] SQL/Python-агрегации MAE, ME, RMSE, hit rate по окнам 7д/30д/сезон/год.
- [ ] Юнит-тесты на синтетических данных (в т.ч. пропуски дней, граница сезона/года, DST).

### Фаза 4 — бот (1 день)
- [ ] `/start`, `/forecast`, `/errors`, allowlist, форматирование.
- [ ] Алерты об ошибках пайплайна в чат.

### Фаза 5 — деплой и обкатка (0.5 дня + 1–2 недели наблюдения)
- [x] Docker-compose на VPS, volume, логи. (Dockerfile: Python 3.12, non-root, БД на volume `/data`; compose: `env_file`, `DB_PATH=/data/weather.db`, ротация логов.)
- [x] Бэкфилл фактов из архива. (`scripts/backfill.py`: CLI-архив CLILAX + добор METAR, приоритет CLI; проверен вживую на окне 6 суток.)
- [ ] 7–14 дней наблюдения: сверять факт с официальным CLI вручную, убедиться в корректности циклов и таймзон. (Выполняется на VPS после деплоя.)

### Фаза 6 — миграция на Yandex Cloud Functions (serverless)
- [x] Слой БД с двумя бэкендами: `sqlite` (локально/тесты) и `ydb` (YDB Serverless) за фасадом `app/db/repo.py`.
- [x] `handler.py`: entrypoint'ы `bot_webhook` (webhook Telegram вместо polling) и `job` (fetch/verify по таймер-триггерам).
- [x] Скрипты: `init_ydb.py` (таблицы), `migrate_to_ydb.py` (перенос истории из SQLite), `set_webhook.py`, `deploy.ps1`.
- [ ] Настройка облака и деплой (раздел 13) — выполняется вручную.
- [ ] После обкатки в облаке: удалить воркфлоу `bot-poll.yml`, `weather-jobs.yml`, `verify-actuals.yml` и файл `data/la_weather.db` из репозитория (до этого GitHub Actions остаётся рабочей схемой хостинга).

**Итого активной разработки: ~4–5 дней.**

---

## 11. Риски и открытые вопросы

| Риск / вопрос | Митигация |
|---|---|
| Пути к бюллетеням NOAA меняются | Конфигурируемые URL, алерты при 404/изменении формата |
| Задержка публикации CLI | Fallback METAR + перезапись при появлении CLI |
| Какой цикл модели считать «зачётным» | Зафиксировано: последний цикл до 00:00 local target-даты; можно хранить все циклы и позже анализировать «ошибку по циклам» |
| °F vs °C для пользователя | Хранить °F, показывать оба |
| Год = календарный или скользящие 365 дней | Решить до Фазы 3 (предложение: скользящие 365 — стабильнее для сравнения) |
| Нужны ли другие станции в будущем | Схема уже содержит модель/дату; добавить колонку `station` при первом расширении |

---

## 12. Что пересмотреть при росте системы

- SQLite → Postgres при нескольких станциях/пользователях или конкурентной записи. (Частично закрыто Фазой 6: в облаке — YDB Serverless.)
- ~~Long polling → webhook при публичном хостинге.~~ Сделано в Фазе 6.
- ~~Добавить графики ошибок (PNG в Telegram).~~ Сделано: `/chart`, лёгкий рендеринг
  через Pillow без временных файлов.

---

## 13. Деплой в Yandex Cloud Functions (Фаза 6)

Архитектура в облаке: три функции из одного архива кода — `la-weather-bot-poll`
(забирает Telegram updates раз в минуту через `getUpdates`, entrypoint
`handler.bot_poll`), резервная `la-weather-bot-webhook` и `la-weather-jobs`
(fetch/verify, entrypoint `handler.job`, вызывается таймер-триггерами). Данные —
в YDB Serverless (`DB_BACKEND=ydb`, см. `app/db/repo.py`). Polling используется
потому, что входящие соединения Telegram к публичным endpoint'ам Yandex Cloud
в этой конфигурации нестабильны; исходящие Bot API-запросы работают.

Разовая настройка (PowerShell, требуется настроенный `yc init`):

```powershell
# 1. Сервисный аккаунт функций: доступ к YDB + право вызова функций для триггеров.
yc iam service-account create --name la-weather-sa
$SA_ID = (yc iam service-account get --name la-weather-sa --format json | ConvertFrom-Json).id
$FOLDER_ID = (yc config get folder-id)
yc resource-manager folder add-access-binding $FOLDER_ID --role ydb.editor --subject serviceAccount:$SA_ID
yc resource-manager folder add-access-binding $FOLDER_ID --role functions.functionInvoker --subject serviceAccount:$SA_ID

# 2. База YDB Serverless; endpoint и путь базы — в .env (YDB_ENDPOINT/YDB_DATABASE).
yc ydb database create la-weather --serverless
yc ydb database get --name la-weather --format json   # поля endpoint / database

# 3. Заполнить .env: YDB_DATABASE, YC_SERVICE_ACCOUNT_ID=$SA_ID,
#    TG_WEBHOOK_SECRET (случайная строка), остальное как раньше.

# 4. Создать таблицы и перенести историю из SQLite (локально, под своим IAM-токеном).
$env:YDB_ACCESS_TOKEN_CREDENTIALS = (yc iam create-token)
python -m scripts.init_ydb
python -m scripts.migrate_to_ydb data/la_weather.db

# 5. Задеплоить функции. Скрипт также создаст минутный polling-триггер.
powershell -File scripts\deploy.ps1
python -m scripts.set_webhook --delete

# 6. Таймер-триггеры (cron в UTC — те же времена, что были в GitHub Actions).
yc serverless trigger create timer --name la-weather-fetch `
  --cron-expression '30 5,11,17,23 ? * * *' --payload '{"job": "fetch"}' `
  --invoke-function-name la-weather-jobs --invoke-function-service-account-id $SA_ID
yc serverless trigger create timer --name la-weather-verify `
  --cron-expression '0 16,22 ? * * *' --payload '{"job": "verify"}' `
  --invoke-function-name la-weather-jobs --invoke-function-service-account-id $SA_ID

```

Повторный деплой после изменения кода — просто `powershell -File scripts\deploy.ps1`.
Webhook должен оставаться снятым, иначе Telegram запретит `getUpdates`.

Проверка после деплоя: послать боту `/forecast` и `/errors` и подождать до минуты;
polling можно дёрнуть вручную — `yc serverless function invoke --name
la-weather-bot-poll --data '{}'`; джобы — `yc serverless function invoke --name
la-weather-jobs --data '{"job": "verify"}'`; логи — `yc serverless function logs
la-weather-bot-poll`.

После 1–2 недель стабильной работы: удалить `.github/workflows/bot-poll.yml`,
`weather-jobs.yml`, `verify-actuals.yml` и `data/la_weather.db` (история живёт в YDB).

---

## 14. Единая таблица дневных ошибок (`model_daily_errors`)

Все дневные ошибки моделей — архивные из интернета и оперативные от
verify-джоба — живут в одной таблице `model_daily_errors` (зачётный прогноз,
факт, ошибка на каждую дату/модель). Из неё читает `/errors`; агрегаты по
окнам 7д/30д/сезон/год считает `app/metrics.py` на лету. Оперативные таблицы
`forecasts`/`actuals` остаются источником для записи оперативных строк, но
не участвуют в `/errors` напрямую.

Два писателя и правило конфликтов:

- `scripts/backfill_daily_errors.py` — интернет-архив: прогнозы из
  [IEM MOS Archive](https://mesonet.agron.iastate.edu/mos/) для KLAX
  (`NBS → NBM`, `GFS → MAV`, `NAM → MET`), фактический Tmax — из
  [NOAA NCEI Daily Summaries](https://www.ncei.noaa.gov/access/services/data/v1)
  для станции `USW00023174`; для каждой локальной даты LA выбирается последний
  цикл до полуночи — то же правило, что в оперативной статистике;
- verify-джоб — оперативные дни (`forecast_source='OPERATIONAL'`) из
  собственных `forecasts`/`actuals` после записи факта.

Оперативная строка не может быть перезаписана архивной (аналог приоритета
CLI над METAR), поэтому бэкфилл можно безопасно перегонять.

Повторяемый импорт последних 365 дней в выбранный `DB_BACKEND`:

```powershell
python -m scripts.backfill_daily_errors --days 365
```
