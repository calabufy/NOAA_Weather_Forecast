# init_ydb.py — разовое создание таблиц в YDB Serverless (аналог schema.sql).
# В отличие от SQLite схема НЕ применяется на каждый connect (DDL в YDB — отдельные
# запросы), поэтому перед первым запуском функций таблицы создаёт этот скрипт.
# Идемпотентен: CREATE TABLE IF NOT EXISTS.
#
# Запуск (из корня проекта, локально):
#   set DB_BACKEND=ydb, YDB_DATABASE=... в .env (или окружении) и токен:
#     PowerShell: $env:YDB_ACCESS_TOKEN_CREDENTIALS = (yc iam create-token)
#   python -m scripts.init_ydb

from __future__ import annotations

import logging

from app.db import ydb_repo

log = logging.getLogger("init_ydb")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    conn = ydb_repo.connect()
    try:
        ydb_repo.init_db(conn)
    finally:
        conn.close()
    log.info(
        "таблицы forecasts/actuals и historical_model_* созданы "
        "(или уже существовали)"
    )


if __name__ == "__main__":
    main()
