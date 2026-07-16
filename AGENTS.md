# AGENTS.md — работа агентов с Yandex Cloud в этом проекте

Инструкция для AI-агентов (Claude Code и др.): как деплоить и обслуживать
serverless-часть проекта. Написана по итогам реальной миграции 2026-07-16 —
все «грабли» ниже случались на практике.

## Архитектура в облаке

Бот работает в Yandex Cloud Functions (каталог `default`), данные — в YDB
Serverless. Классическая схема из README §13:

| Ресурс | Имя / id | Назначение |
|---|---|---|
| Функция webhook | `la-weather-bot-webhook` (id `d4emlk08aqd9464fc39t`) | Апдейты Telegram, entrypoint `handler.bot_webhook`, timeout 120s |
| Функция джобов | `la-weather-jobs` | fetch/verify, entrypoint `handler.job`, timeout 300s |
| YDB Serverless | база `la-weather` | Таблицы `forecasts`, `actuals` (+ `historical_model_*`) |
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
