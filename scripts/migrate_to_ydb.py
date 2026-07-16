# migrate_to_ydb.py — разовый перенос накопленной истории из SQLite в YDB.
# Читает forecasts/actuals из файла SQLite (по умолчанию data/la_weather.db —
# база, которую коммитили GitHub Actions) и безусловно UPSERT'ит строки в YDB
# как есть, включая исходные fetched_at и source (правило «CLI приоритетнее
# METAR» здесь не нужно: в исходной базе оно уже применено).
#
# Идемпотентен: повторный запуск перезапишет те же строки теми же значениями.
#
# Запуск (из корня проекта, локально; окружение — как для scripts/init_ydb.py):
#   python -m scripts.migrate_to_ydb [путь-к-sqlite]

from __future__ import annotations

import argparse
import logging
import sqlite3

from app.db import ydb_repo

log = logging.getLogger("migrate_to_ydb")


def migrate(sqlite_path: str) -> tuple[int, int]:
    """Перенести все строки; вернуть (число прогнозов, число фактов)."""
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dst = ydb_repo.connect()
    try:
        forecasts = src.execute(
            "SELECT target_date, model, cycle, tmax_f, fetched_at FROM forecasts"
        ).fetchall()
        for i, row in enumerate(forecasts, 1):
            ydb_repo.upsert_forecast_row(
                dst, row["target_date"], row["model"], row["cycle"],
                row["tmax_f"], row["fetched_at"],
            )
            if i % 100 == 0:
                log.info("прогнозы: %d/%d", i, len(forecasts))

        actuals = src.execute(
            "SELECT date, tmax_f, source, fetched_at FROM actuals"
        ).fetchall()
        for row in actuals:
            ydb_repo.upsert_actual_row(
                dst, row["date"], row["tmax_f"], row["source"], row["fetched_at"]
            )
        return len(forecasts), len(actuals)
    finally:
        dst.close()
        src.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    p = argparse.ArgumentParser(description="Перенос истории из SQLite в YDB.")
    p.add_argument(
        "sqlite_path", nargs="?", default="data/la_weather.db",
        help="путь к исходному файлу SQLite (default: %(default)s)",
    )
    args = p.parse_args()

    n_forecasts, n_actuals = migrate(args.sqlite_path)
    log.info("перенесено: %d прогнозов, %d фактов", n_forecasts, n_actuals)


if __name__ == "__main__":
    main()
