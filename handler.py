# handler.py — точки входа Yandex Cloud Functions (см. README, раздел деплоя).
# Замена main.py для serverless-режима: вместо одного вечного процесса с long
# polling и APScheduler — две функции из одного архива кода:
#
#   handler.bot_webhook — HTTP-вызов от Telegram (webhook). Функция публичная,
#     поэтому запрос сверяется с TG_WEBHOOK_SECRET (заголовок
#     X-Telegram-Bot-Api-Secret-Token, задаётся при setWebhook).
#   handler.job — вызывается таймер-триггерами; какой джоб гонять (fetch/verify),
#     говорит payload триггера.
#
# Dispatcher/router создаются один раз на процесс (тёплые вызовы переиспользуют),
# а Bot — на каждый вызов: его aiohttp-сессия привязана к event loop'у, и мы не
# полагаемся на то, что runtime сохраняет loop между вызовами. Данные — в YDB
# (DB_BACKEND=ydb задаётся окружением функции; см. app/db/repo.py).

from __future__ import annotations

import asyncio
import base64
import json
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import Update

from app import config
from app.bot import handlers, middleware
from app.db import repo

logging.basicConfig(
    level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger(__name__)

# Алерты пайплайна (ERROR+ из app.jobs/app.sources) — прямой отправкой в чат:
# постоянного loop бота в serverless нет. Установка идемпотентна.
middleware.install_sync_alert_handler(config.BOT_TOKEN, config.ALLOWED_CHAT_IDS)

_JOB_NAMES = frozenset({"fetch", "verify"})


def _job_runner(name: str):
    """Импортировать тяжёлый код джоба только в jobs-функции.

    Обе Cloud Functions используют один архив и модуль entrypoint. Глобальные
    импорты jobs/sources замедляли холодный старт Telegram webhook, хотя боту
    этот код не нужен.
    """
    if name == "fetch":
        from app.jobs import fetch_forecasts

        return fetch_forecasts.run
    if name == "verify":
        from app.jobs import verify

        return verify.run
    raise ValueError(f"неизвестный джоб {name!r} (ожидается {sorted(_JOB_NAMES)})")

# Dispatcher без состояния соединений — безопасно шарить между тёплыми вызовами.
_dp = Dispatcher()
_dp.include_router(handlers.router)
_dp.message.middleware(middleware.AllowlistMiddleware(config.ALLOWED_CHAT_IDS))


# --- Telegram webhook --------------------------------------------------------

def _header(event: dict, name: str) -> str | None:
    """Достать HTTP-заголовок без учёта регистра (YCF отдаёт как прислали)."""
    headers = event.get("headers") or {}
    lname = name.lower()
    for key, value in headers.items():
        if key.lower() == lname:
            return value
    return None


def _http_body(event: dict) -> str:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body).decode("utf-8")
    return body


async def bot_webhook(event: dict, context) -> dict:
    """Обработать один апдейт Telegram, присланный на webhook.

    Всегда отвечаем 200 (кроме неверного секрета): при не-200 Telegram будет
    ретраить тот же апдейт, а повторная доставка сломанного апдейта бессмысленна —
    ошибка уже залогирована и заалерчена.
    """
    if config.TG_WEBHOOK_SECRET:
        got = _header(event, "X-Telegram-Bot-Api-Secret-Token")
        if got != config.TG_WEBHOOK_SECRET:
            log.warning("webhook: неверный секрет — запрос отвергнут")
            return {"statusCode": 403, "body": "forbidden"}

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        update = Update.model_validate_json(_http_body(event))
        await _dp.feed_update(bot, update)
    except Exception:  # noqa: BLE001 — см. докстринг: ретраи Telegram не помогут
        log.exception("webhook: сбой обработки апдейта")
    finally:
        await bot.session.close()
    return {"statusCode": 200, "body": "ok"}


# --- Telegram timer polling -------------------------------------------------

async def _poll_once() -> int:
    """Забрать и обработать одну пачку Telegram updates.

    Резервный режим для сетей, из которых Telegram не может стабильно вызвать
    публичный webhook Yandex Cloud. После успешной обработки всей пачки второй
    getUpdates подтверждает offset; при аварии Telegram отдаст пачку повторно.
    """
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        try:
            updates = await bot.get_updates(
                limit=100,
                timeout=0,
                allowed_updates=["message"],
                request_timeout=8,
            )
        except TelegramNetworkError as exc:
            # Стандартный aiohttp timeout aiogram — около минуты. При временной
            # недоступности Telegram он перекрывал следующий минутный trigger и
            # превращал задержку ответа в несколько минут. Следующий poll скоро
            # повторится, поэтому завершаем этот вызов штатно и быстро.
            log.warning("polling: getUpdates недоступен, повторю: %s", exc)
            return 0
        for update in updates:
            log.info("polling: обрабатываю update_id=%s", update.update_id)
            await _dp.feed_update(bot, update)
        if updates:
            # offset=N подтверждает все update_id < N. Возвращённые этим
            # запросом более новые апдейты останутся неподтверждёнными и будут
            # обработаны следующим минутным запуском.
            await bot.get_updates(
                offset=updates[-1].update_id + 1,
                limit=1,
                timeout=0,
                allowed_updates=["message"],
                request_timeout=8,
            )
        return len(updates)
    finally:
        await bot.session.close()


def bot_poll(event: dict, context) -> dict:
    """YCF entrypoint для timer-trigger: один короткий getUpdates-запрос."""
    count = asyncio.run(_poll_once())
    log.info("polling: обработано обновлений: %s", count)
    return {"processed": count}


# --- Фоновые джобы (таймер-триггеры) ----------------------------------------

def _job_name(event: dict | None) -> str:
    """Имя джоба из события: прямой вызов {'job': ...} или payload триггера.

    Payload таймер-триггера приходит строкой внутри messages[].details.payload;
    поддерживаем и голое имя ('fetch'), и JSON ('{"job": "fetch"}').
    """
    event = event or {}
    if event.get("job"):
        return str(event["job"])
    for message in event.get("messages", []):
        payload = (message.get("details") or {}).get("payload") or ""
        if not payload:
            continue
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict) and parsed.get("job"):
                return str(parsed["job"])
        except ValueError:
            pass
        return payload.strip()
    raise ValueError(f"не удалось определить джоб из события: {event!r}")


def job(event: dict, context) -> dict:
    """Запустить один фоновый джоб (fetch/verify) — вызывается таймер-триггером."""
    name = _job_name(event)
    run = _job_runner(name)
    conn = repo.connect()
    try:
        result = run(conn)
    finally:
        conn.close()
    log.info("джоб %s завершён: %s", name, result)
    # str: результат verify — dataclass (ActualTmax), в JSON-ответ он не сериализуется.
    return {"job": name, "result": str(result)}
