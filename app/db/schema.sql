-- schema.sql — DDL-схема БД SQLite.
-- Таблицы forecasts (прогнозы Tmax по моделям и циклам) и actuals (фактический
-- Tmax суток с указанием источника CLI/METAR). Ограничения обеспечивают
-- идемпотентность записи. Применяется при инициализации БД в repo.py.

-- Прогнозы Tmax. Одна строка = один прогноз конкретной модели из конкретного
-- цикла на конкретные локальные сутки LA. Повторный сбор того же цикла обновляет
-- строку (idempotent upsert по ключу target_date+model+cycle).
CREATE TABLE IF NOT EXISTS forecasts (
    id           INTEGER PRIMARY KEY,
    target_date  TEXT NOT NULL,   -- локальная дата LA, YYYY-MM-DD
    model        TEXT NOT NULL,   -- 'NBM' | 'MAV' | 'MET'
    cycle        TEXT NOT NULL,   -- цикл модели, ISO-UTC 'YYYY-MM-DDTHH:MMZ'
    tmax_f       REAL NOT NULL,   -- прогноз Tmax, °F
    fetched_at   TEXT NOT NULL,   -- момент записи, ISO-UTC
    UNIQUE(target_date, model, cycle)
);

-- Выбор «зачётного» прогноза дня — последний цикл модели до local midnight
-- target-даты — фильтрует по (target_date, model) и сортирует по cycle.
CREATE INDEX IF NOT EXISTS idx_forecasts_target_model
    ON forecasts(target_date, model, cycle);

-- Фактический Tmax суток. Одна строка на локальную дату LA. CLI приоритетнее
-- METAR: METAR-факт может быть перезаписан пришедшим позже CLI, но не наоборот
-- (правило реализовано в upsert_actual).
CREATE TABLE IF NOT EXISTS actuals (
    date       TEXT PRIMARY KEY,  -- локальная дата LA, YYYY-MM-DD
    tmax_f     REAL NOT NULL,     -- факт Tmax, °F
    source     TEXT NOT NULL,     -- 'CLI' | 'METAR'
    fetched_at TEXT NOT NULL      -- момент записи, ISO-UTC
);

-- Изолированный интернет-архив. Одна строка = зачётный прогноз (последний цикл
-- до local midnight target_date) + официальный факт. Эти строки никогда не
-- участвуют в оперативных /forecast и /errors.
CREATE TABLE IF NOT EXISTS historical_model_daily (
    target_date       TEXT NOT NULL,
    model             TEXT NOT NULL,
    cycle             TEXT NOT NULL,
    forecast_tmax_f   REAL NOT NULL,
    actual_tmax_f     REAL NOT NULL,
    error_f           REAL NOT NULL,
    abs_error_f       REAL NOT NULL,
    forecast_source   TEXT NOT NULL,
    actual_source     TEXT NOT NULL,
    imported_at       TEXT NOT NULL,
    PRIMARY KEY (target_date, model)
);

CREATE INDEX IF NOT EXISTS idx_historical_model_daily_model_date
    ON historical_model_daily(model, target_date);

-- Готовые агрегаты за импортированный период: по одной строке на модель.
CREATE TABLE IF NOT EXISTS historical_model_metrics (
    model               TEXT NOT NULL,
    period_start        TEXT NOT NULL,
    period_end          TEXT NOT NULL,
    n                   INTEGER NOT NULL,
    mae                 REAL NOT NULL,
    bias                REAL NOT NULL,
    rmse                REAL NOT NULL,
    hit_rate_1f         REAL NOT NULL,
    hit_rate_2f         REAL NOT NULL,
    hit_rate_3f         REAL NOT NULL,
    max_abs_error       REAL NOT NULL,
    max_abs_error_date  TEXT NOT NULL,
    forecast_source     TEXT NOT NULL,
    actual_source       TEXT NOT NULL,
    computed_at         TEXT NOT NULL,
    PRIMARY KEY (model, period_start, period_end)
);
