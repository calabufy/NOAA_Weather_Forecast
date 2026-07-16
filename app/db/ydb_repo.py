# ydb_repo.py — YDB-реализация репозитория (см. фасад app/db/repo.py).
# Хранилище для Yandex Cloud Functions: YDB Serverless, таблицы forecasts/actuals
# (та же логическая схема, что в schema.sql для SQLite). Публичный API идентичен
# sqlite_repo.py; «соединение» — лёгкая обёртка над общим на процесс драйвером.
#
# Драйвер и пул сессий создаются один раз на процесс и переиспользуются между
# тёплыми вызовами функции (создание драйвера — сотни миллисекунд, на каждый
# вызов заново — расточительно). close() у обёртки поэтому — no-op.
#
# Аутентификация — через ydb.credentials_from_env_variables():
#   - в Cloud Functions: YDB_METADATA_CREDENTIALS=1 (IAM сервисного аккаунта функции);
#   - локально: YDB_ACCESS_TOKEN_CREDENTIALS=$(yc iam create-token) или
#     YDB_SERVICE_ACCOUNT_KEY_FILE_CREDENTIALS=<путь к ключу SA>.

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timezone

import ydb

from app import config, timeutil
from app.sources import ActualTmax, ForecastPoint

UTC = timezone.utc
log = logging.getLogger(__name__)

# Общие на процесс драйвер и пул (тёплые вызовы функции их переиспользуют).
_lock = threading.Lock()
_driver: ydb.Driver | None = None
_pool: ydb.QuerySessionPool | None = None


# --- Форматирование дат/времени (те же канонические строки, что в SQLite) ---

def _fmt_date(d: date) -> str:
    return d.isoformat()


def _fmt_cycle(dt: datetime) -> str:
    """Цикл модели -> 'YYYY-MM-DDTHH:MMZ' (aware приводится к UTC)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Соединение --------------------------------------------------------------

class Connection:
    """Непрозрачное «соединение» фасада repo: держит ссылку на общий пул.

    close() ничего не делает — драйвер живёт столько же, сколько процесс,
    чтобы тёплые вызовы Cloud Function не платили за реконнект.
    """

    def __init__(self, pool: ydb.QuerySessionPool) -> None:
        self.pool = pool

    def close(self) -> None:
        pass


def connect(db_path: str | None = None) -> Connection:
    """Вернуть соединение с YDB (параметр db_path игнорируется — сигнатура фасада).

    Первый вызов на процесс создаёт драйвер по config.YDB_ENDPOINT/YDB_DATABASE
    и ждёт его готовности; повторные — переиспользуют пул.
    В отличие от SQLite схему здесь НЕ применяем на каждый connect (DDL в YDB —
    отдельные запросы к scheme-сервису): создание таблиц — scripts/init_ydb.py.
    """
    global _driver, _pool
    with _lock:
        if _pool is None:
            if not config.YDB_ENDPOINT or not config.YDB_DATABASE:
                raise RuntimeError(
                    "YDB_ENDPOINT/YDB_DATABASE не заданы — заполните окружение "
                    "(см. .env.example) или используйте DB_BACKEND=sqlite"
                )
            _driver = ydb.Driver(
                endpoint=config.YDB_ENDPOINT,
                database=config.YDB_DATABASE,
                credentials=ydb.credentials_from_env_variables(),
            )
            _driver.wait(fail_fast=True, timeout=15)
            _pool = ydb.QuerySessionPool(_driver)
        return Connection(_pool)


def init_db(conn: Connection) -> None:
    """Создать таблицы, если их ещё нет (идемпотентно, аналог schema.sql)."""
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS forecasts (
            target_date  Utf8 NOT NULL,   -- локальная дата LA, YYYY-MM-DD
            model        Utf8 NOT NULL,   -- 'NBM' | 'MAV' | 'MET'
            cycle        Utf8 NOT NULL,   -- цикл модели, ISO-UTC 'YYYY-MM-DDTHH:MMZ'
            tmax_f       Double NOT NULL, -- прогноз Tmax, °F
            fetched_at   Utf8 NOT NULL,   -- момент записи, ISO-UTC
            PRIMARY KEY (target_date, model, cycle)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS actuals (
            `date`     Utf8 NOT NULL,     -- локальная дата LA, YYYY-MM-DD
            tmax_f     Double NOT NULL,   -- факт Tmax, °F
            `source`   Utf8 NOT NULL,     -- 'CLI' | 'METAR'
            fetched_at Utf8 NOT NULL,     -- момент записи, ISO-UTC
            PRIMARY KEY (`date`)
        )
        """,
    ]
    for statement in ddl:
        conn.pool.execute_with_retries(statement)


def _query(conn: Connection, text: str, params: dict | None = None) -> list:
    """Выполнить запрос с ретраями, вернуть строки первого result set."""
    result_sets = conn.pool.execute_with_retries(text, params)
    return result_sets[0].rows if result_sets else []


# --- Запись прогнозов --------------------------------------------------------

def upsert_forecast_row(
    conn: Connection,
    target_date: str,
    model: str,
    cycle: str,
    tmax_f: float,
    fetched_at: str,
) -> None:
    """Низкоуровневый безусловный UPSERT строки прогноза (используется миграцией)."""
    conn.pool.execute_with_retries(
        """
        UPSERT INTO forecasts (target_date, model, cycle, tmax_f, fetched_at)
        VALUES ($target_date, $model, $cycle, $tmax_f, $fetched_at)
        """,
        {
            "$target_date": target_date,
            "$model": model,
            "$cycle": cycle,
            "$tmax_f": float(tmax_f),
            "$fetched_at": fetched_at,
        },
    )


def upsert_forecast(conn: Connection, fp: ForecastPoint) -> None:
    """Идемпотентно записать прогноз (ключ target_date+model+cycle, как в SQLite)."""
    upsert_forecast_row(
        conn, _fmt_date(fp.target_date), fp.model, _fmt_cycle(fp.cycle),
        float(fp.tmax_f), _now_iso(),
    )


def upsert_forecasts(conn: Connection, points: list[ForecastPoint]) -> int:
    """Выполнить upsert пачки и вернуть число обработанных точек."""
    for fp in points:
        upsert_forecast(conn, fp)
    return len(points)


# --- Запись фактов -----------------------------------------------------------

def upsert_actual_row(
    conn: Connection, d: str, tmax_f: float, source: str, fetched_at: str
) -> None:
    """Низкоуровневый безусловный UPSERT строки факта (используется миграцией)."""
    conn.pool.execute_with_retries(
        """
        UPSERT INTO actuals (`date`, tmax_f, `source`, fetched_at)
        VALUES ($date, $tmax_f, $source, $fetched_at)
        """,
        {
            "$date": d,
            "$tmax_f": float(tmax_f),
            "$source": source,
            "$fetched_at": fetched_at,
        },
    )


def upsert_actual(conn: Connection, actual: ActualTmax) -> bool:
    """Записать фактический Tmax суток с приоритетом CLI над METAR.

    Правило то же, что в SQLite: CLI всегда перезаписывает; METAR пишется, только
    если по этой дате ещё нет CLI. Проверка и запись — два запроса без общей
    транзакции: это безопасно, потому что писатель actuals один (verify-джоб,
    один инстанс по таймеру), гонок нет.
    """
    if actual.source != "CLI":
        existing = get_actual(conn, actual.date)
        if existing is not None and existing.source == "CLI":
            return False
    upsert_actual_row(
        conn, _fmt_date(actual.date), float(actual.tmax_f), actual.source, _now_iso()
    )
    return True


# --- Выборки -----------------------------------------------------------------

def official_forecast(
    conn: Connection, target_date: date, model: str
) -> ForecastPoint | None:
    """«Зачётный» прогноз дня: последний цикл модели до local midnight target-даты."""
    midnight_utc = timeutil.local_day_bounds(target_date)[0]
    rows = _query(
        conn,
        """
        SELECT target_date, model, cycle, tmax_f
        FROM forecasts
        WHERE target_date = $target_date AND model = $model AND cycle < $cutoff
        ORDER BY cycle DESC
        LIMIT 1
        """,
        {
            "$target_date": _fmt_date(target_date),
            "$model": model,
            "$cutoff": _fmt_cycle(midnight_utc),
        },
    )
    return _row_to_forecast(rows[0]) if rows else None


def latest_forecast(
    conn: Connection, target_date: date, model: str
) -> ForecastPoint | None:
    """Самый свежий прогноз на target_date (без отсечки по local midnight)."""
    rows = _query(
        conn,
        """
        SELECT target_date, model, cycle, tmax_f
        FROM forecasts
        WHERE target_date = $target_date AND model = $model
        ORDER BY cycle DESC
        LIMIT 1
        """,
        {"$target_date": _fmt_date(target_date), "$model": model},
    )
    return _row_to_forecast(rows[0]) if rows else None


def get_actual(conn: Connection, d: date) -> ActualTmax | None:
    """Фактический Tmax локальных суток d, если он записан."""
    rows = _query(
        conn,
        "SELECT `date`, tmax_f, `source` FROM actuals WHERE `date` = $date",
        {"$date": _fmt_date(d)},
    )
    return _row_to_actual(rows[0]) if rows else None


def list_actuals(conn: Connection, start: date, end: date) -> list[ActualTmax]:
    """Все факты с датой в [start, end] включительно, по возрастанию даты."""
    rows = _query(
        conn,
        """
        SELECT `date`, tmax_f, `source` FROM actuals
        WHERE `date` BETWEEN $start AND $end
        ORDER BY `date`
        """,
        {"$start": _fmt_date(start), "$end": _fmt_date(end)},
    )
    return [_row_to_actual(r) for r in rows]


def error_series(
    conn: Connection,
    model: str,
    start: date,
    end: date,
    *,
    actuals: list[ActualTmax] | None = None,
) -> list[tuple[date, float, float]]:
    """Пары (дата, зачётный_прогноз_f, факт_f) за [start, end] для модели.

    Логика идентична sqlite_repo.error_series: все циклы модели одним запросом,
    порог local midnight (индивидуален для даты, учитывает DST) — в Python.
    """
    actuals = actuals if actuals is not None else list_actuals(conn, start, end)
    if not actuals:
        return []

    rows = _query(
        conn,
        """
        SELECT target_date, cycle, tmax_f
        FROM forecasts
        WHERE model = $model AND target_date BETWEEN $start AND $end
        ORDER BY target_date, cycle DESC
        """,
        {"$model": model, "$start": _fmt_date(start), "$end": _fmt_date(end)},
    )
    cutoffs = {
        actual.date: _fmt_cycle(timeutil.local_day_bounds(actual.date)[0])
        for actual in actuals
    }
    official: dict[date, float] = {}
    for row in rows:
        target = date.fromisoformat(row["target_date"])
        if target not in official and row["cycle"] < cutoffs.get(target, ""):
            official[target] = row["tmax_f"]

    return [
        (actual.date, official[actual.date], actual.tmax_f)
        for actual in actuals
        if actual.date in official
    ]


# --- Преобразование строк ----------------------------------------------------

def _row_to_forecast(row) -> ForecastPoint:
    return ForecastPoint(
        target_date=date.fromisoformat(row["target_date"]),
        model=row["model"],
        cycle=datetime.strptime(row["cycle"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC),
        tmax_f=row["tmax_f"],
    )


def _row_to_actual(row) -> ActualTmax:
    return ActualTmax(
        date=date.fromisoformat(row["date"]),
        tmax_f=row["tmax_f"],
        source=row["source"],
    )
