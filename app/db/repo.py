# repo.py — фасад репозитория: выбирает реализацию хранилища по config.DB_BACKEND.
#   - 'sqlite' (дефолт): app/db/sqlite_repo.py — локальная разработка и тесты.
#   - 'ydb': app/db/ydb_repo.py — YDB Serverless для Yandex Cloud Functions.
#
# Обе реализации предоставляют один и тот же публичный API; вызывающий код
# (джобы, хендлеры, скрипты) импортирует только этот модуль и не знает, какое
# хранилище под ним. «Соединение» — непрозрачный объект: передавайте то, что
# вернул connect(), обратно в функции репозитория и закрывайте close().

from __future__ import annotations

from app import config

if config.DB_BACKEND == "ydb":
    from app.db import ydb_repo as _impl
elif config.DB_BACKEND == "sqlite":
    from app.db import sqlite_repo as _impl
else:
    raise RuntimeError(
        f"неизвестный DB_BACKEND={config.DB_BACKEND!r} (ожидается 'sqlite' или 'ydb')"
    )

connect = _impl.connect
init_db = _impl.init_db
upsert_forecast = _impl.upsert_forecast
upsert_forecasts = _impl.upsert_forecasts
upsert_actual = _impl.upsert_actual
official_forecast = _impl.official_forecast
latest_forecast = _impl.latest_forecast
get_actual = _impl.get_actual
list_actuals = _impl.list_actuals
error_series = _impl.error_series
upsert_historical_days = _impl.upsert_historical_days
upsert_historical_metrics = _impl.upsert_historical_metrics
historical_error_series = _impl.historical_error_series
