# Dockerfile — альтернативный постоянный режим для VPS (основной деплой сейчас
# выполняется короткими сессиями GitHub Actions): базовый Python 3.12, зависимости из
# pyproject.toml, копирование пакета app/ и запуск app/main.py как единого процесса
# (бот + планировщик). БД лежит на volume, монтируемом через docker-compose.

FROM python:3.12-slim

# Логи — без буферизации (сразу в docker logs), .pyc не пишем в образ.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Сначала только манифест + пакет, нужные для сборки: установка зависимостей
# кэшируется отдельным слоем и не пересобирается при правке scripts/ и т.п.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

# Разовый бэкфилл-скрипт (запускается вручную: docker compose run --rm bot
# python -m scripts.backfill ...), не входит в пакет.
COPY scripts ./scripts

# БД — на volume /data (см. docker-compose.yml), чтобы forecasts/actuals
# переживали пересборку образа. Каталог создаём и отдаём непривилегированному
# пользователю: процесс пишет SQLite не из-под root.
ENV DB_PATH=/data/weather.db
RUN mkdir -p /data && useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /data /app
USER appuser
VOLUME ["/data"]

# Единый процесс: бот (long polling) + APScheduler в общем asyncio-loop.
CMD ["python", "-m", "app.main"]
