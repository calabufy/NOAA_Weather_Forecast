# nws.py — доступ к api.weather.gov.
# Point forecast (/gridpoints/LOX/...) как опциональный третий прогноз и
# наблюдения METAR (/stations/KLAX/observations) как fallback-источник факта
# для верификации, когда CLI-отчёт ещё не опубликован.
