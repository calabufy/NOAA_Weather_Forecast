# fetch_forecasts.py — Forecast Fetcher.
# По расписанию (после выхода циклов 00Z/12Z) вызывает источники NBM/MAV/NWS,
# получает Tmax на target-дату и идемпотентно пишет в таблицу forecasts
# (ключ target_date+model+cycle).
