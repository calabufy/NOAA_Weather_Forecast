# middleware.py — сквозная логика бота.
# Allowlist по chat_id (бот личный — чужие игнорируются) и отправка алертов об
# ошибках пайплайна (парсинг/сбор данных) в тот же телеграм-чат.

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any, Awaitable, Callable

import httpx
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

log = logging.getLogger(__name__)
_TELEGRAM_ALERT_TEXT_LIMIT = 3500
_TELEGRAM_SEND_TIMEOUT = 10.0


class AllowlistMiddleware(BaseMiddleware):
    """Пропускает апдейты только от разрешённых chat_id (бот личный).

    Чужие сообщения молча игнорируются: не отвечаем, чтобы не подтверждать
    существование бота. Пустой allowlist => не пройдёт никто (безопасный дефолт).
    """

    def __init__(self, allowed_chat_ids: frozenset[int]) -> None:
        self._allowed = allowed_chat_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat = getattr(event, "chat", None)
        chat_id = getattr(chat, "id", None)
        if chat_id not in self._allowed:
            log.info("апдейт от постороннего chat_id=%s — игнорирую", chat_id)
            return None
        return await handler(event, data)


class TelegramAlertHandler(logging.Handler):
    """logging.Handler, пересылающий записи уровня ERROR+ в телеграм-чат(ы).

    Так «алерты об ошибках пайплайна» из джобов и источников (они логируют через
    log.exception/error) доходят до владельца, не связывая синхронные джобы с
    async-ботом напрямую. Джобы работают в отдельных потоках (asyncio.to_thread),
    поэтому отправку планируем в loop бота через run_coroutine_threadsafe.

    Ставит себя на корневой логгер; отправка неблокирующая и «best-effort» —
    ошибка самой отправки гасится (иначе рекурсивный лог об ошибке лога).

    Пересылаются только «свои» ошибки — записи от логгеров пайплайна (см.
    ALERT_LOGGER_PREFIXES). Шум сторонних библиотек (напр. таймауты long polling
    от aiogram) в чат не уходит: это не сбой пайплайна, а транзиентная сеть.
    """

    # Белый список: шлём в чат только ошибки нашего кода (джобы/источники).
    ALERT_LOGGER_PREFIXES = ("app.jobs", "app.sources")

    def __init__(
        self,
        bot: Any,
        loop: asyncio.AbstractEventLoop,
        chat_ids: frozenset[int],
        level: int = logging.ERROR,
    ) -> None:
        super().__init__(level=level)
        self._bot = bot
        self._loop = loop
        self._chat_ids = chat_ids

    def emit(self, record: logging.LogRecord) -> None:
        # Только «свои» ошибки пайплайна; всё остальное (включая собственные
        # сбои отправки из app.bot.middleware и шум aiogram) — мимо.
        if not record.name.startswith(self.ALERT_LOGGER_PREFIXES):
            return
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001 — форматирование лога не должно падать
            return
        # Телеграм-лимит длины сообщения — обрезаем с запасом.
        escaped = html.escape(text[:_TELEGRAM_ALERT_TEXT_LIMIT], quote=False)
        text = f"⚠️ Ошибка пайплайна:\n<pre>{escaped}</pre>"
        for chat_id in self._chat_ids:
            asyncio.run_coroutine_threadsafe(
                self._safe_send(chat_id, text), self._loop
            )

    async def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id, text)
        except Exception:  # noqa: BLE001 — best-effort, сбой алерта не критичен
            log.warning("не удалось отправить алерт в chat_id=%s", chat_id)


class SyncTelegramAlertHandler(logging.Handler):
    """Синхронный вариант TelegramAlertHandler для Yandex Cloud Functions.

    В serverless-режиме нет постоянно живущего loop бота, на который можно было
    бы планировать отправку (джобы — обычный синхронный код, webhook-вызов
    завершается вместе с ответом), поэтому алерт шлётся прямым HTTP-запросом к
    Bot API. Фильтр логгеров и «best-effort»-семантика те же, что у async-версии.
    """

    ALERT_LOGGER_PREFIXES = TelegramAlertHandler.ALERT_LOGGER_PREFIXES

    def __init__(
        self, bot_token: str, chat_ids: frozenset[int], level: int = logging.ERROR
    ) -> None:
        super().__init__(level=level)
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_ids = chat_ids

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith(self.ALERT_LOGGER_PREFIXES):
            return
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001 — форматирование лога не должно падать
            return
        escaped = html.escape(text[:_TELEGRAM_ALERT_TEXT_LIMIT], quote=False)
        text = f"⚠️ Ошибка пайплайна:\n<pre>{escaped}</pre>"
        for chat_id in self._chat_ids:
            try:
                httpx.post(
                    self._url,
                    json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                    timeout=_TELEGRAM_SEND_TIMEOUT,
                )
            except Exception:  # noqa: BLE001 — сбой алерта не критичен
                log.warning("не удалось отправить алерт в chat_id=%s", chat_id)


def install_sync_alert_handler(
    bot_token: str, chat_ids: frozenset[int]
) -> SyncTelegramAlertHandler | None:
    """Повесить SyncTelegramAlertHandler на корневой логгер (идемпотентно).

    Возвращает установленный handler (или None: пустой allowlist / нет токена /
    уже установлен — повторная установка дублировала бы алерты).
    """
    if not bot_token or not chat_ids:
        log.warning("sync-алерты выключены: нет токена или allowlist пуст")
        return None
    root = logging.getLogger()
    if any(isinstance(h, SyncTelegramAlertHandler) for h in root.handlers):
        return None
    handler = SyncTelegramAlertHandler(bot_token, chat_ids)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    return handler


def install_alert_handler(
    bot: Any, loop: asyncio.AbstractEventLoop, chat_ids: frozenset[int]
) -> TelegramAlertHandler | None:
    """Повесить TelegramAlertHandler на корневой логгер (если есть кому слать).

    Возвращает установленный handler (или None при пустом allowlist).
    """
    if not chat_ids:
        log.warning("allowlist пуст — алерты пайплайна отправлять некому")
        return None
    handler = TelegramAlertHandler(bot, loop, chat_ids)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(handler)
    return handler
