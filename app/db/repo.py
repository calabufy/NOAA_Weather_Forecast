# repo.py — репозиторий доступа к SQLite.
# Инициализирует БД по schema.sql, предоставляет идемпотентные upsert'ы прогнозов
# и фактов, а также выборки (в т.ч. «зачётный» прогноз дня — последний цикл до
# local midnight target-даты) для метрик и команд бота.
#
# Все функции чистые относительно ввода: принимают открытое соединение sqlite3
# и не держат глобального состояния. Даты хранятся как ISO-строки ('YYYY-MM-DD'),
# цикл и метки времени — как ISO-UTC ('YYYY-MM-DDTHH:MMZ'), чтобы лексикографическая
# сортировка совпадала с хронологической.

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from app import config, timeutil
from app.sources import ActualTmax, ForecastPoint

UTC = timezone.utc

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


# --- Форматирование дат/времени в каноничные ISO-строки --------------------

def _fmt_date(d: date) -> str:
    return d.isoformat()


def _fmt_cycle(dt: datetime) -> str:
    """Цикл модели -> 'YYYY-MM-DDTHH:MMZ' (aware приводится к UTC)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Инициализация ---------------------------------------------------------

def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Открыть соединение с БД и применить схему.

    db_path=None -> берётся config.DB_PATH; ':memory:' поддерживается для тестов.
    Включает foreign_keys и row_factory=sqlite3.Row (доступ к колонкам по имени).
    """
    conn = sqlite3.connect(db_path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Применить schema.sql (идемпотентно: все CREATE ... IF NOT EXISTS)."""
    conn.executescript(_SCHEMA_PATH.read_text())
    conn.commit()


# --- Запись прогнозов ------------------------------------------------------

def upsert_forecast(conn: sqlite3.Connection, fp: ForecastPoint) -> None:
    """Идемпотентно записать прогноз.

    Ключ (target_date, model, cycle): повторный сбор того же цикла обновляет
    tmax_f и fetched_at, не создавая дубликат.
    """
    conn.execute(
        """
        INSERT INTO forecasts (target_date, model, cycle, tmax_f, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(target_date, model, cycle) DO UPDATE SET
            tmax_f     = excluded.tmax_f,
            fetched_at = excluded.fetched_at
        """,
        (_fmt_date(fp.target_date), fp.model, _fmt_cycle(fp.cycle),
         float(fp.tmax_f), _now_iso()),
    )


def upsert_forecasts(conn: sqlite3.Connection, points: list[ForecastPoint]) -> int:
    """Записать пачку прогнозов и закоммитить; вернуть число записанных строк."""
    for fp in points:
        upsert_forecast(conn, fp)
    conn.commit()
    return len(points)


# --- Запись фактов ---------------------------------------------------------

def upsert_actual(conn: sqlite3.Connection, actual: ActualTmax) -> bool:
    """Записать фактический Tmax суток с приоритетом CLI над METAR.

    Правило: CLI всегда перезаписывает; METAR пишется только если по этой дате
    ещё нет CLI (иначе поздний METAR-повтор затёр бы канонический CLI).
    Возвращает True, если строка была вставлена/обновлена.
    """
    cur = conn.execute(
        """
        INSERT INTO actuals (date, tmax_f, source, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            tmax_f     = excluded.tmax_f,
            source     = excluded.source,
            fetched_at = excluded.fetched_at
        WHERE excluded.source = 'CLI' OR actuals.source <> 'CLI'
        """,
        (_fmt_date(actual.date), float(actual.tmax_f), actual.source, _now_iso()),
    )
    conn.commit()
    return cur.rowcount > 0


# --- Выборки ---------------------------------------------------------------

def official_forecast(
    conn: sqlite3.Connection, target_date: date, model: str
) -> ForecastPoint | None:
    """«Зачётный» прогноз дня: последний цикл модели до local midnight target-даты.

    Правило фиксировано (см. README §4): сравниваем цикл с полуночью target-суток
    в зоне станции, приведённой к UTC, чтобы прогнозы разных дней были сравнимы.
    """
    midnight_utc = timeutil.local_day_bounds(target_date)[0]
    row = conn.execute(
        """
        SELECT target_date, model, cycle, tmax_f
        FROM forecasts
        WHERE target_date = ? AND model = ? AND cycle < ?
        ORDER BY cycle DESC
        LIMIT 1
        """,
        (_fmt_date(target_date), model, _fmt_cycle(midnight_utc)),
    ).fetchone()
    return _row_to_forecast(row) if row else None


def get_actual(conn: sqlite3.Connection, d: date) -> ActualTmax | None:
    """Фактический Tmax локальных суток d, если он записан."""
    row = conn.execute(
        "SELECT date, tmax_f, source FROM actuals WHERE date = ?",
        (_fmt_date(d),),
    ).fetchone()
    if row is None:
        return None
    return ActualTmax(
        date=date.fromisoformat(row["date"]),
        tmax_f=row["tmax_f"],
        source=row["source"],
    )


def _row_to_forecast(row: sqlite3.Row) -> ForecastPoint:
    return ForecastPoint(
        target_date=date.fromisoformat(row["target_date"]),
        model=row["model"],
        cycle=datetime.strptime(row["cycle"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC),
        tmax_f=row["tmax_f"],
    )
