# set_webhook.py — установка/снятие webhook Telegram для Yandex Cloud Function.
# Разовая операция после деплоя функции bot-webhook: сообщает Telegram публичный
# URL функции и секрет (handler.bot_webhook сверяет заголовок
# X-Telegram-Bot-Api-Secret-Token с TG_WEBHOOK_SECRET). Заодно регистрирует меню
# команд бота (раньше это делал main.py на старте polling-сессии).
#
# Webhook и polling у Telegram взаимоисключающие: пока webhook установлен,
# polling-сессии (GitHub Actions / локальный main.py) апдейтов не получат.
# Откат на polling: python -m scripts.set_webhook --delete
#
# Запуск (BOT_TOKEN и TG_WEBHOOK_SECRET — из .env/окружения):
#   python -m scripts.set_webhook https://functions.yandexcloud.net/<function-id>

from __future__ import annotations

import argparse
import logging

import httpx

from app import config
from app.bot.handlers import BOT_COMMANDS

log = logging.getLogger("set_webhook")


def _call(method: str, payload: dict) -> dict:
    """Вызов метода Bot API; при ok=false — исключение с описанием от Telegram."""
    resp = httpx.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}",
        json=payload,
        timeout=config.HTTP_TIMEOUT,
    )
    data = resp.json()
    if not data.get("ok"):
        raise SystemExit(f"{method}: Telegram ответил ошибкой: {data}")
    return data


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # httpx логирует полный URL запроса; для Bot API он содержит секретный токен.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(description="Установить/снять webhook Telegram.")
    p.add_argument("url", nargs="?", help="публичный URL функции bot-webhook")
    p.add_argument(
        "--delete", action="store_true",
        help="снять webhook (вернуться к long polling); накопленные апдейты не теряются",
    )
    args = p.parse_args()

    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан — заполните .env (см. .env.example)")

    if args.delete:
        _call("deleteWebhook", {"drop_pending_updates": False})
        log.info("webhook снят — можно возвращаться к long polling")
        return

    if not args.url:
        raise SystemExit("укажите URL функции (или --delete для снятия webhook)")
    if not config.TG_WEBHOOK_SECRET:
        log.warning("TG_WEBHOOK_SECRET пуст — webhook будет принимать запросы без проверки")

    payload: dict = {
        "url": args.url,
        "drop_pending_updates": False,
        # Последовательная доставка важна для serverless: при холодном старте
        # несколько одновременных апдейтов создают несколько тяжёлых инстансов,
        # а Telegram успевает оборвать соединения до ответа функции.
        "max_connections": 1,
        # Бот обрабатывает только сообщения — не будим функцию ради прочего.
        "allowed_updates": ["message"],
    }
    if config.TG_WEBHOOK_SECRET:
        payload["secret_token"] = config.TG_WEBHOOK_SECRET
    _call("setWebhook", payload)

    _call("setMyCommands", {
        "commands": [
            {"command": c.command, "description": c.description} for c in BOT_COMMANDS
        ],
    })
    log.info("webhook установлен: %s (+ меню команд обновлено)", args.url)


if __name__ == "__main__":
    main()
