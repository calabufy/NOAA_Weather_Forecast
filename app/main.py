# main.py — точка входа сервиса.
# Инициализирует БД, поднимает планировщик APScheduler с джобами (fetch/verify)
# и запускает Telegram-бота (long polling) в одном общем asyncio-loop.
# Всё приложение — один процесс.
