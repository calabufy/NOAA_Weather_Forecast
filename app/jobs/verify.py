# verify.py — Verification Job.
# Утром по LA-времени забирает фактический Tmax за вчера из CLI-отчёта и пишет
# в actuals. Если CLI ещё нет — fallback на METAR (source='METAR') с перезаписью
# на CLI позже (CLI приоритетнее).
