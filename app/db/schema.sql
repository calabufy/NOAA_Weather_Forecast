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
