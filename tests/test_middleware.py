# test_middleware.py — тесты сквозной логики бота.
# Здесь: белый список TelegramAlertHandler — в чат уходят только ошибки
# пайплайна (app.jobs/app.sources), а шум сторонних библиотек (aiogram) — нет.

import logging

from app.bot import middleware


def _record(name: str, msg: str = "boom") -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=logging.ERROR, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )


def _handler_capturing(monkeypatch) -> tuple[middleware.TelegramAlertHandler, list]:
    """Хендлер, у которого планирование отправки перехвачено в список."""
    scheduled: list = []

    def fake_schedule(coro, loop):
        coro.close()  # не оставляем «never awaited» корутину
        scheduled.append(coro)

    monkeypatch.setattr(middleware.asyncio, "run_coroutine_threadsafe", fake_schedule)
    handler = middleware.TelegramAlertHandler(
        bot=object(), loop=object(), chat_ids=frozenset({1})
    )
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    return handler, scheduled


def test_pipeline_errors_are_forwarded(monkeypatch):
    handler, scheduled = _handler_capturing(monkeypatch)
    handler.emit(_record("app.jobs.fetch"))
    handler.emit(_record("app.sources.nbm"))
    assert len(scheduled) == 2  # по одному send на chat_id из allowlist


def test_aiogram_noise_is_not_forwarded(monkeypatch):
    handler, scheduled = _handler_capturing(monkeypatch)
    handler.emit(_record("aiogram.dispatcher", "Request timeout error"))
    assert scheduled == []


def test_own_send_failures_do_not_recurse(monkeypatch):
    handler, scheduled = _handler_capturing(monkeypatch)
    handler.emit(_record("app.bot.middleware"))
    assert scheduled == []
