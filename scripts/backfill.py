# backfill.py — разовый скрипт бэкфилла фактов.
# Загружает исторический фактический Tmax из архива CF6/CLI, чтобы окна 30д/сезон
# наполнились быстрее. Прогнозы задним числом не восстанавливаются — статистика
# ошибок копится только с момента запуска сервиса.
#
# Запуск (из корня проекта):
#   python -m scripts.backfill --days 45
#   python -m scripts.backfill --start 2026-06-01 --end 2026-06-30 --source cli
#   docker compose run --rm bot python -m scripts.backfill --days 45
#
# Источники факта (тот же приоритет, что в верификации — CLI канонический):
#   * CLI  — архив продуктов CLILAX офиса LOX (api.weather.gov/products). Достаёт
#            факты за десятки дней назад, ограничен лишь ретеншеном API.
#   * METAR — почасовые наблюдения станции; api.weather.gov хранит их недолго
#            (~неделя), поэтому это добор самых свежих суток, где CLI ещё нет.
# upsert_actual не даёт METAR затереть CLI, поэтому порядок записи безопасен.

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from app import config, timeutil
from app.db import repo
from app.sources import ActualTmax, ParseError, cli_report, http_get_json, nws

log = logging.getLogger("backfill")


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _date_range(d0: date, d1: date) -> list[date]:
    """Список дат [d0, d1] включительно, по возрастанию."""
    return [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]


def cli_archive(cli_limit: int) -> dict[date, ActualTmax]:
    """Скачать архив CLILAX офиса LOX -> {дата: канонический факт CLI}.

    Листинг продуктов отсортирован от свежих к старым; для одной даты берём
    первый (самый свежий — учитывает возможные corrected-выпуски CLI).
    """
    listing_url = (
        f"https://api.weather.gov/products?type=CLI"
        f"&office={config.CLI_OFFICE}&limit={cli_limit}"
    )
    by_date: dict[date, ActualTmax] = {}
    listing = http_get_json(listing_url)
    for item in listing.get("@graph", []):
        product_id = item["@id"].rsplit("/", 1)[-1]
        try:
            obj = http_get_json(config.CLI_PRODUCT_URL.format(product_id=product_id))
        except Exception:  # noqa: BLE001 — один сбойный продукт не рушит бэкфилл
            log.warning("не удалось получить CLI-продукт %s", product_id)
            continue
        text = obj.get("productText", "")
        if not cli_report._is_lax(text):  # чужая станция офиса — пропускаем
            continue
        try:
            actual = cli_report.parse_cli_tmax(text)
        except ParseError as e:
            log.warning("CLI-продукт %s не распознан: %s", product_id, e)
            continue
        by_date.setdefault(actual.date, actual)
    return by_date


def backfill(
    conn,
    d0: date,
    d1: date,
    source: str,
    cli_limit: int,
) -> tuple[int, int]:
    """Записать факты за [d0, d1]. Вернуть (записано_CLI, записано_METAR)."""
    days = _date_range(d0, d1)
    have: set[date] = set()  # даты, где факт уже записан в этом прогоне
    n_cli = n_metar = 0

    if source in ("cli", "both"):
        archive = cli_archive(cli_limit)
        for d in days:
            actual = archive.get(d)
            if actual is None:
                continue
            if repo.upsert_actual(conn, actual):
                n_cli += 1
                have.add(d)
                log.info("CLI  %s: %.0f°F", d.isoformat(), actual.tmax_f)

    if source in ("metar", "both"):
        for d in days:
            if d in have:  # CLI уже закрыл эти сутки
                continue
            try:
                actual = nws.fetch_actual(d)
            except Exception:  # noqa: BLE001 — старые сутки вне ретеншена и т.п.
                log.warning("METAR за %s недоступен", d.isoformat())
                continue
            if actual is None:  # нет наблюдений в окне суток
                continue
            if repo.upsert_actual(conn, actual):
                n_metar += 1
                log.info("METAR %s: %.1f°F", d.isoformat(), actual.tmax_f)

    return n_cli, n_metar


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    p = argparse.ArgumentParser(
        description="Бэкфилл исторических фактов Tmax (CLI + METAR) в БД."
    )
    p.add_argument("--days", type=int, default=30,
                   help="сколько последних суток забэкфиллить (по умолчанию 30); "
                        "игнорируется, если заданы --start/--end")
    p.add_argument("--start", type=_parse_date, help="начало диапазона YYYY-MM-DD")
    p.add_argument("--end", type=_parse_date, help="конец диапазона YYYY-MM-DD (вкл.)")
    p.add_argument("--source", choices=("cli", "metar", "both"), default="both",
                   help="источник факта (по умолчанию both: CLI + добор METAR)")
    p.add_argument("--cli-limit", type=int, default=200,
                   help="сколько продуктов CLI офиса запросить из архива (default 200)")
    args = p.parse_args()

    # Диапазон дат: явный [start, end] либо последние N суток до вчерашней.
    # Сегодняшние сутки не берём — они ещё не закрыты (нет финального Tmax).
    if args.start or args.end:
        if not (args.start and args.end):
            p.error("--start и --end задаются вместе")
        if args.start > args.end:
            p.error("--start не может быть позже --end")
        d0, d1 = args.start, args.end
    else:
        d1 = timeutil.la_today() - timedelta(days=1)
        d0 = d1 - timedelta(days=args.days - 1)

    log.info("бэкфилл фактов за %s..%s (source=%s)",
             d0.isoformat(), d1.isoformat(), args.source)
    conn = repo.connect()
    try:
        n_cli, n_metar = backfill(conn, d0, d1, args.source, args.cli_limit)
    finally:
        conn.close()
    total_days = (d1 - d0).days + 1
    log.info("готово: записано CLI=%d, METAR=%d из %d суток диапазона",
             n_cli, n_metar, total_days)


if __name__ == "__main__":
    main()
