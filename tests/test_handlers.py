import asyncio

from app.bot import handlers


class LoadingMessage:
    def __init__(self, *, delete_fails: bool = False):
        self.delete_fails = delete_fails
        self.deleted = False

    async def delete(self):
        self.deleted = True
        if self.delete_fails:
            raise RuntimeError("already deleted")


class Message:
    def __init__(self, *, delete_fails: bool = False):
        self.answers = []
        self.loading = LoadingMessage(delete_fails=delete_fails)

    async def answer(self, text):
        self.answers.append(text)
        return self.loading


def test_forecast_failure_returns_message_and_delete_does_not_mask(monkeypatch):
    async def fail():
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(handlers.live, "forecast_tomorrow", fail)
    message = Message(delete_fails=True)

    asyncio.run(handlers.cmd_forecast(message))

    assert message.answers == [
        "Загрузка прогноза...",
        "Не удалось получить прогноз, попробуйте позже.",
    ]
    assert message.loading.deleted
