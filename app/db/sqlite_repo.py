# sqlite_repo.py — SQLite-реализация репозитория (см. фасад app/db/repo.py).
# Инициализирует БД по schema.sql, предоставляет идемпотентные upsert'ы прогнозов
# и фактов, а также выборки (в т.ч. «зачётный» прогноз дня — последний цикл до
# local midnight target-даты) для метрик и команд бота.
# Используется локально (разработка, тесты) и как источник при миграции в YDB.
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
    """Выполнить upsert пачки, закоммитить и вернуть число обработанных точек."""
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


def latest_forecast(
    conn: sqlite3.Connection, target_date: date, model: str
) -> ForecastPoint | None:
    """Самый свежий прогноз на target_date (последний по циклу), для показа в боте.

    В отличие от official_forecast здесь НЕТ отсечки «до local midnight»: команда
    /forecast показывает пользователю актуальнейший собранный прогноз на завтра,
    а не «зачётный» для метрик. Для target-даты «завтра» оба правила обычно дают
    один и тот же цикл, но семантику показа держим отдельной от верификации.
    """
    row = conn.execute(
        """
        SELECT target_date, model, cycle, tmax_f
        FROM forecasts
        WHERE target_date = ? AND model = ?
        ORDER BY cycle DESC
        LIMIT 1
        """,
        (_fmt_date(target_date), model),
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


def list_actuals(
    conn: sqlite3.Connection, start: date, end: date
) -> list[ActualTmax]:
    """Все факты с датой в [start, end] включительно, по возрастанию даты."""
    rows = conn.execute(
        """
        SELECT date, tmax_f, source FROM actuals
        WHERE date BETWEEN ? AND ?
        ORDER BY date
        """,
        (_fmt_date(start), _fmt_date(end)),
    ).fetchall()
    return [
        ActualTmax(date=date.fromisoformat(r["date"]), tmax_f=r["tmax_f"],
                   source=r["source"])
        for r in rows
    ]


def error_series(
    conn: sqlite3.Connection,
    model: str,
    start: date,
    end: date,
    *,
    actuals: list[ActualTmax] | None = None,
) -> list[tuple[date, float, float]]:
    """Пары (дата, зачётный_прогноз_f, факт_f) за [start, end] для модели.

    Только дни с полными данными: есть и факт, и «зачётный» прогноз (последний
    цикл до local midnight). Заготовка для метрик (app/metrics.py) и команды
    /errors — сама агрегация чистая и живёт в metrics.
    """
    actuals = actuals if actuals is not None else list_actuals(conn, start, end)
    if not actuals:
        return []

    # Все циклы модели получаем одним запросом. Порог local midnight остаётся в
    # Python, поскольку он индивидуален для даты и должен корректно учитывать DST.
    rows = conn.execute(
        """
        SELECT target_date, cycle, tmax_f
        FROM forecasts
        WHERE model = ? AND target_date BETWEEN ? AND ?
        ORDER BY target_date, cycle DESC
        """,
        (model, _fmt_date(start), _fmt_date(end)),
    ).fetchall()
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


def _row_to_forecast(row: sqlite3.Row) -> ForecastPoint:
    return ForecastPoint(
        target_date=date.fromisoformat(row["target_date"]),
        model=row["model"],
        cycle=datetime.strptime(row["cycle"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC),
        tmax_f=row["tmax_f"],
    )
