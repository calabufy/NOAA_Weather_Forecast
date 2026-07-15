# run_job.py — точка входа для разового запуска фоновых джобов вне сервиса.
# Нужен для GitHub Actions: там app/jobs/fetch_forecasts.py и app/jobs/verify.py
# не годятся как точка входа сами по себе (только объявляют run(conn, ...),
# вызов — задача scheduler.py внутри постоянно работающего процесса). Здесь —
# тонкая обёртка по образцу scripts/backfill.py: открыть соединение, вызвать
# нужный джоб один раз, закрыть.
#
# Запуск (из корня проекта):
#   python -m scripts.run_job fetch
#   python -m scripts.run_job verify

from __future__ import annotations

import argparse
import logging

from app.db import repo
from app.jobs import fetch_forecasts, verify

log = logging.getLogger("run_job")

JOBS = {
    "fetch": fetch_forecasts.run,
    "verify": verify.run,
}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    p = argparse.ArgumentParser(description="Разовый запуск фонового джоба.")
    p.add_argument("job", choices=sorted(JOBS))
    args = p.parse_args()

    conn = repo.connect()
    try:
        result = JOBS[args.job](conn)
    finally:
        conn.close()
    log.info("джоб %s завершён: %s", args.job, result)


if __name__ == "__main__":
    main()
