# AGENTS.md — гид по проекту для агентов

Инструкция для AI-агентов (Claude Code и др.): архитектура приложения, как
деплоить и обслуживать serverless-часть. Раздел про облако написан по итогам
реальной миграции 2026-07-16 — все «грабли» там случались на практике.

## Что это за приложение

Личный сервис прогноза максимальной дневной температуры (Tmax) на станции
**KLAX** (аэропорт Лос-Анджелеса): ежедневно собирает прогнозы трёх
статистических моделей NOAA (**NBM**, **MAV**, **MET**), после окончания суток
сверяет их с официальным фактом и копит статистику ошибок. Интерфейс — личный
Telegram-бот с командами `/forecast` (прогноз на завтра), `/errors` (метрики
качества по окнам 7д/30д/сезон/год), `/chart` (PNG-сравнение моделей), `/help`,
`/start`. Подробная предметная
документация — README.md (это исходный план проекта, он же спецификация).

## Доменные правила (инварианты — не нарушать)

1. **Сутки станции**: все «дни» — климатические сутки в зоне
   `America/Los_Angeles` (local midnight → midnight, с учётом DST). Логика —
   только через `app/timeutil.py`.
2. **Температура хранится в °F** (нативные единицы NOAA); °C — только на
   отображении.
3. **Форматы дат в БД — ISO-строки**: дата `YYYY-MM-DD`, цикл модели и метки
   времени `YYYY-MM-DDTHH:MMZ` (UTC). Лексикографический порядок = хронология —
   на этом построены все `ORDER BY cycle`.
4. **«Зачётный» прогноз дня** (для метрик) — последний цикл модели строго до
   local midnight target-даты (`repo.official_forecast`). Для показа в боте —
   просто самый свежий (`repo.latest_forecast`). Это разные вещи, не путать.
5. **Приоритет фактов**: CLI (официальный климатический отчёт) всегда
   перезаписывает METAR-фолбэк, но не наоборот (`repo.upsert_actual`).
6. **Идемпотентность**: все записи — upsert'ы по естественным ключам; любой
   джоб можно перезапускать без вреда.
7. **Изоляция сбоев**: отказ одного источника/модели не роняет остальные
   (`fetch_all_isolated`); ошибки уровня ERROR из `app.jobs`/`app.sources`
   уходят алертами в Telegram владельцу.
8. **Allowlist**: бот отвечает только chat_id из `ALLOWED_CHAT_IDS`; чужим не
   отвечает вовсе (не подтверждаем существование бота).
9. **Обработчики бота должны быть быстрыми** (< пары секунд): Telegram ждёт
   ответ webhook ≤60 с, тяжёлые операции — только в джобах по таймеру.

## Карта кода

```
handler.py            входы Yandex Cloud Functions: bot_webhook (Telegram) и job (fetch/verify)
app/
  main.py             legacy-вход: long polling + APScheduler (локальная отладка, GH Actions)
  config.py           ВСЯ конфигурация: env-переменные, URL источников, расписания, константы станции
  timeutil.py         климатические сутки LA: границы, la_today/la_tomorrow (UTC↔local, DST)
  metrics.py          чистые агрегации ошибок (MAE, ME/bias, RMSE, hit rate) по окнам
  sources/            скачивание+парсинг, БЕЗ записи в БД; каждый модуль возвращает чистые данные
    __init__.py       ForecastPoint, ActualTmax, ParseError, http-хелперы, fetch_all_isolated
    nbm.py mav.py met.py   прогнозы: бюллетени NBS (bulk ~28 МБ), GFSMAV, NAMMET
    cli_report.py     факт: официальный CLI-отчёт CLILAX (канонический)
    nws.py            факт-фолбэк: расчёт Tmax по METAR-наблюдениям api.weather.gov
  db/
    repo.py           фасад: выбирает реализацию по DB_BACKEND ('sqlite'|'ydb'); публичный API один
    sqlite_repo.py    SQLite (локально/тесты); schema.sql — DDL
    ydb_repo.py       YDB Serverless (прод в облаке); драйвер — singleton на процесс
    daily_errors.py   ModelDayError + OPERATIONAL_SOURCE (единая таблица дневных ошибок)
  jobs/
    fetch_forecasts.py  сбор прогнозов текущего цикла (latest_cycle с лагом публикации)
    verify.py           факт за вчера: CLI → фолбэк METAR
    scheduler.py        legacy APScheduler для main.py (в облаке заменён таймер-триггерами)
  bot/
    handlers.py       команды; /forecast, /errors и /chart читают ТОЛЬКО из БД
    charts.py         лёгкий PNG-график метрик через Pillow, без временных файлов
    formatting.py     весь HTML-текст сообщений
    middleware.py     allowlist + алерты (async-вариант для polling, sync — для serverless)
    live.py           legacy: «живой» забор прогноза; в webhook-режиме НЕ использовать
scripts/
  run_job.py          разовый запуск fetch/verify (использовался в GH Actions)
  backfill.py         добор фактов за прошлые дни (CLI-архив + METAR)
  backfill_daily_errors.py  импорт годовой истории из IEM MOS Archive + NCEI в model_daily_errors
  init_ydb.py migrate_to_ydb.py set_webhook.py build_zip.py deploy.ps1   обслуживание облака (см. ниже)
tests/                pytest; парсеры гоняются на образцах из tests/fixtures/, БД — sqlite ':memory:'
```

Слоистость строгая: `sources` не знают про БД; `jobs` — оркестрация
source→repo; `metrics` — чистые функции; `bot` читает только `repo`/`metrics`.
Новый код должен сохранять это разделение.

## Модель данных

Оперативные таблицы (пишутся джобами, читаются ботом):
- `forecasts(target_date, model, cycle, tmax_f, fetched_at)` — PK/уникальность
  `(target_date, model, cycle)`; все циклы всех моделей.
- `actuals(date, tmax_f, source, fetched_at)` — PK `date`; source `CLI|METAR`.

Единая таблица дневных ошибок (из неё читает `/errors`; агрегаты по окнам
считает `app/metrics.py` на лету):
- `model_daily_errors` — зачётный прогноз+факт+ошибка на день, PK
  `(target_date, model)`. Два писателя: `scripts/backfill_daily_errors.py`
  (интернет-архив IEM/NCEI) и verify-джоб (оперативные дни,
  `forecast_source='OPERATIONAL'`). Оперативную строку архив не
  перезаписывает (правило в `repo.upsert_daily_errors`).

Схема живёт в ДВУХ местах: `app/db/schema.sql` (SQLite) и DDL в
`ydb_repo.init_db` (YDB). Меняешь одну — меняй вторую и прогоняй
`python -m scripts.init_ydb` для облака.

## Режимы запуска

| Режим | Как | Когда |
|---|---|---|
| Прод (облако) | функции из `handler.py`, webhook + таймер-триггеры | основной |
| Локальная отладка | `python -m app.main` (long polling; снять webhook `set_webhook --delete`!) | разработка |
| Разовый джоб локально | `python -m scripts.run_job fetch\|verify` | отладка джобов |
| GH Actions (legacy) | воркфлоу в `.github/workflows/` | резерв до вывода из эксплуатации |

Polling и webhook у Telegram взаимоисключающие: пока установлен webhook,
локальный polling не получит апдейтов (и наоборот, поднятый polling собьёт
доставку в облако). Всегда возвращай webhook после локальной отладки.

## Конвенции и качество

- Python ≥3.10, ruff (`line-length 100`, правила E4/E7/E9/F/I); комментарии и
  докстринги — по-русски, в стиле существующих (объясняют «почему», не «что»).
- Перед любым коммитом/деплоем: `python -m pytest -q` и `python -m ruff check .`
  (зависимости — в `.venv`; тестам YDB не нужна, дефолтный бэкенд sqlite).
- Типовые задачи:
  - новая команда бота → `handlers.py` (+ `BOT_COMMANDS`) + `formatting.py`
    (+ регистрация меню: перезапустить `set_webhook`); помнить про правило №9;
  - новый источник данных → модуль в `sources/` по образцу соседей + фикстура
    в `tests/fixtures/` + тест парсера;
  - изменение схемы БД → schema.sql + ydb_repo.init_db + обе реализации repo +
    `init_ydb` + редеплой;
  - новая переменная окружения → `config.py` + `.env.example` + `deploy.ps1`
    (блок `$envArgs`) + редеплой.

## Архитектура в облаке

Бот работает в Yandex Cloud Functions (каталог `default`), данные — в YDB
Serverless. Классическая схема из README §13:

| Ресурс | Имя / id | Назначение |
|---|---|---|
| Функция webhook | `la-weather-bot-webhook` (id `d4emlk08aqd9464fc39t`) | Апдейты Telegram, entrypoint `handler.bot_webhook`, timeout 120s |
| Функция джобов | `la-weather-jobs` | fetch/verify, entrypoint `handler.job`, timeout 300s |
| YDB Serverless | база `la-weather` | Таблицы `forecasts`, `actuals`, `model_daily_errors` |
| Таймер-триггеры | `la-weather-fetch`, `la-weather-verify` | cron UTC `30 5,11,17,23 ? * * *` и `0 16,22 ? * * *`, payload `{"job": "fetch"|"verify"}` |
| Сервисный аккаунт | `la-weather-sa` (id `ajeg2rtbnjkrq6nmj38r`) | Роли `ydb.editor`, `functions.functionInvoker`; функции ходят в YDB через него |

URL webhook-функции: `https://functions.yandexcloud.net/<id>`; Telegram
дополнительно шлёт секрет в заголовке `X-Telegram-Bot-Api-Secret-Token`
(значение — `TG_WEBHOOK_SECRET` из `.env`).

## Деплой

Единственный поддерживаемый путь — из PowerShell:

```powershell
powershell -File scripts\deploy.ps1    # или .\scripts\deploy.ps1
```

Скрипт: читает `.env` → собирает zip через `python scripts/build_zip.py`
(app/ + handler.py + requirements.txt) → создаёт функции при отсутствии →
заливает версии обеих функций с переменными окружения → печатает URL webhook.

Требования к `.env` (проверяются скриптом): `BOT_TOKEN`, `ALLOWED_CHAT_IDS`,
`YDB_ENDPOINT`, `YDB_DATABASE`, `TG_WEBHOOK_SECRET`, `YC_SERVICE_ACCOUNT_ID`.
Пустых значений Яндекс не принимает («Illegal value of environment variable»):
переменные без значения не передавать вовсе.

После изменения схемы БД дополнительно: `python -m scripts.init_ydb`
(DDL в YDB не применяется на connect — только этим скриптом).

## Управление и диагностика

```powershell
# разовый запуск джоба в облаке (сквозной тест: код + YDB + NOAA)
yc serverless function invoke --name la-weather-jobs --data '{\"job\": \"verify\"}'

# состояние webhook у Telegram: pending_update_count и last_error_message
# (TOKEN — из .env)
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"

# установка/снятие webhook (снятие возвращает бота на long polling)
python -m scripts.set_webhook https://functions.yandexcloud.net/<id>
python -m scripts.set_webhook --delete
```

Логи: `yc serverless function logs <имя> --since 30m` — команда **часто висит
минуты** (уводить в фон, писать в файл). Каждое исполнение видно по парам
START/END/REPORT; `Function Init Duration ~4.5s` в REPORT = холодный старт,
`Code: 499 Request cancelled` = клиент (Telegram) оборвал соединение, не дождавшись.

Синтетический тест webhook без Telegram — POST на URL функции с заголовком
секрета и телом-апдейтом; чужой `chat_id` отфильтруется allowlist'ом без
исходящих запросов (ответ 200), неверный секрет даст 403.

## Жёсткие ограничения, которые нельзя нарушать кодом

- **Telegram ждёт ответ webhook максимум 60 секунд**, потом рвёт соединение и
  ретраит апдейт. Никаких долгих операций в обработчиках бота: `/forecast`
  читает готовое из YDB (пополняет fetch-джоб), «живой» забор бюллетеней
  (`app/bot/live.py`) в webhook-режиме запрещён — он уже устраивал завал очереди.
- Webhook установлен с `max_connections=1` (см. `scripts/set_webhook.py`) —
  последовательная доставка, чтобы ретраи не плодили параллельные холодные
  старты. Не поднимать без причины.
- Связность Telegram ↔ Яндекс эпизодически даёт «Connection timed out» на
  стороне Telegram — это сеть, не код; ретраи Telegram компенсируют. Прямые
  запросы к функции при этом проходят.

## Грабли окружения (Windows, путь проекта со скобками `[ FILES ]`)

Критично: путь проекта содержит `[` `]`, и это ломает инструменты по-разному.

1. **PowerShell 5.1**: относительные пути ломаются целиком (даже с
   `-LiteralPath`), пока текущая директория «скобочная»; cwd дочерних
   процессов может оказаться `System32`. Правило: в `.ps1` — только абсолютные
   пути от `$PSScriptRoot` и только `-LiteralPath` (см. scripts/deploy.ps1).
2. **Кодировка `.ps1`**: обязательно UTF-8 **с BOM**, иначе PS 5.1 читает
   русские комментарии как ANSI-мусор и падает парсер. После правки .ps1
   агентским редактором BOM может слететь — вернуть и проверить парсером.
3. **`Compress-Archive` запрещён** для сборки архива функций: YCF распаковывает
   его с нечитаемыми правами, рантайм падает с `No module named 'app'`.
   Только `scripts/build_zip.py` (явные права 0644/0755).
4. **Git Bash (MSYS)** конвертирует аргументы, начинающиеся с `/`
   (`/ru-central1/...` → `C:/Program Files/Git/ru-central1/...`). Значения для
   `--environment` передавать из PowerShell, либо `MSYS_NO_PATHCONV=1`.
   Значения из `.env` в bash чистить от `\r` (`tr -d '\r'`).
5. **`yc` не в PATH** инструментных шеллов: `C:\Users\Arcturus\yandex-cloud\bin`.
6. **Python проекта** — `.venv` в корне (все зависимости там); системный
   `python` пуст. В PowerShell пользователя активировать
   `.\.venv\Scripts\Activate.ps1`.
7. `yc iam create-token` выводит личный IAM-токен — не светить в выводе,
   присваивать переменной окружения `YDB_ACCESS_TOKEN_CREDENTIALS` (нужен
   локальным скриптам init_ydb/migrate; в облаке функции используют
   `YDB_METADATA_CREDENTIALS=1` + сервисный аккаунт).

## Правила безопасности процесса

- **Переключение webhook (`set_webhook`) — только с явного согласия
  пользователя**: это go-live/откат живого бота.
- GitHub Actions воркфлоу (`bot-poll.yml`, `weather-jobs.yml`,
  `verify-actuals.yml`) и `data/la_weather.db` — резервный контур на время
  обкатки облака. Не удалять, пока пользователь не подтвердит стабильность
  (план — 1–2 недели после переключения).
- Пока webhook активен, `bot-poll` в Actions падает на конфликте getUpdates —
  это ожидаемо, не «чинить».
- Перед деплоем: `python -m pytest -q` и `python -m ruff check .` должны быть
  зелёными (бэкенд по умолчанию sqlite — YDB для тестов не нужна).
