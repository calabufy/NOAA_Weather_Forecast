# formatting.py — форматирование ответов бота.
# Конвертация °F -> °C для отображения и сборка компактных моноширинных таблиц
# метрик. Отделено от handlers, чтобы форматирование можно было тестировать.
#
# Модуль чистый: не импортирует aiogram и БД, работает только над доменными
# объектами (ForecastPoint, WindowStats) и датами. Вывод — строки с HTML-разметкой
# Telegram (parse_mode=HTML); таблицы заворачиваются в <pre> (моноширинный блок
# со скроллом по горизонтали, поэтому ширину колонок можно не ужимать до предела).

from __future__ import annotations

import html
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app import config, timeutil
from app.metrics import HIT_THRESHOLDS_F, WINDOWS, WindowStats
from app.sources import ForecastPoint
from app.sources.polymarket import TempMarket
from app.timeutil import LA

# Человекочитаемые подписи окон (порядок совпадает с metrics.WINDOWS).
WINDOW_LABELS = {"7d": "7д", "30d": "30д", "season": "сез", "year": "год"}

# Зона для перевода расписания джобов (заданного в локальном времени LA) в МСК.
MSK = ZoneInfo("Europe/Moscow")

# Диапазоны Polymarket с вероятностью ниже порога не показываем (длинные хвосты
# по полдоллара оборота), кроме тех, куда попал прогноз какой-либо из моделей.
MARKET_MIN_PROB_SHOWN = 0.01


# --- Единицы и мелкие подписи ----------------------------------------------

def f_to_c(f: float) -> float:
    """°F -> °C (хранение в °F, показ — оба)."""
    return (f - 32.0) * 5.0 / 9.0


def temp_label(f: float) -> str:
    """'84°F (28.9°C)' — температура в обеих шкалах."""
    return f"{round(f)}°F ({f_to_c(f):.1f}°C)"


def cycle_label(cycle: datetime) -> str:
    """Момент цикла модели -> '12Z' (час выпуска в UTC)."""
    return f"{cycle.hour:02d}Z"


def date_label(d: date) -> str:
    """Локальная дата -> 'Sun 12 Jul' (как в примере README)."""
    return d.strftime("%a %d %b")


# --- /forecast -------------------------------------------------------------

def format_forecast(
    target_date: date, points: dict[str, ForecastPoint | None]
) -> str:
    """Прогноз Tmax на завтра по моделям.

    points — {модель: ForecastPoint | None}; None означает «прогноз ещё не
    собран» (честное сообщение вместо пустоты). Порядок моделей — как в
    переданном словаре (вызывающий формирует его по config.BOT_MODELS).
    """
    head = f"Прогноз Tmax на завтра ({date_label(target_date)}, {config.STATION}):"
    lines = [f"<b>{html.escape(head)}</b>"]
    for model, fp in points.items():
        if fp is None:
            lines.append(f"{model}: —  (прогноз ещё не собран)")
        else:
            lines.append(
                f"{model}: {temp_label(fp.tmax_f)}, цикл {cycle_label(fp.cycle)}"
            )
    return "\n".join(lines)


# --- Рынок Polymarket (вторая часть ответа /forecast) ------------------------

def _market_rows(
    market: TempMarket, points: dict[str, ForecastPoint | None]
) -> list[str]:
    """Строки таблицы диапазонов: подпись, вероятность, метки моделей.

    Показываются диапазоны с вероятностью >= MARKET_MIN_PROB_SHOWN, а также
    любые диапазоны, куда попал прогноз модели (даже «хвостовые»), — стрелка
    с именами моделей отмечает, на что фактически «ставит» каждый прогноз.
    """
    marks: dict[str, list[str]] = {}
    for model, fp in points.items():
        if fp is None:
            continue
        bucket = market.bucket_for(fp.tmax_f)
        if bucket is not None:
            marks.setdefault(bucket.label, []).append(model)

    shown = [
        b for b in market.buckets
        if b.prob >= MARKET_MIN_PROB_SHOWN or b.label in marks
    ]
    width = max((len(b.label) for b in shown), default=0)
    rows = []
    for b in shown:
        pct = round(b.prob * 100)
        prob_cell = f"{pct}%" if pct >= 1 else "<1%"
        row = f"{b.label.ljust(width)}  {prob_cell.rjust(4)}"
        if b.label in marks:
            row += "  ← " + ", ".join(marks[b.label])
        rows.append(row)
    return rows


def format_market(
    market: TempMarket | None, points: dict[str, ForecastPoint | None]
) -> str:
    """Блок Polymarket для /forecast: вероятности диапазонов и ссылка на ставку.

    market=None — рынок недоступен (ещё не создан или сбой запроса): честная
    строка вместо блока. points — те же прогнозы, что показаны выше по тексту;
    диапазон с прогнозом модели помечается стрелкой с её именем.
    """
    if market is None:
        return (
            "Polymarket: рынок на эти сутки недоступен "
            "(ещё не создан или сбой запроса)."
        )
    head = (
        f"Polymarket — ставки на Tmax "
        f"({date_label(market.target_date)}, {config.STATION}):"
    )
    table = "\n".join(_market_rows(market, points))
    foot = (
        f"Объём ${market.volume_usd:,.0f} · "
        f'<a href="{html.escape(market.url, quote=True)}">открыть рынок</a>'
    )
    return f"<b>{html.escape(head)}</b>\n<pre>{html.escape(table)}</pre>\n{foot}"


# --- /errors ---------------------------------------------------------------

def _cell(value: float | None, *, signed: bool = False) -> str:
    """Число или '—' при отсутствии данных; signed добавляет знак (для bias)."""
    if value is None:
        return "—"
    return f"{value:+.1f}" if signed else f"{value:.1f}"


def _hit_cells(stats: WindowStats) -> list[str]:
    """Доли hit-rate по фиксированным порогам как целые проценты ('83' / '—')."""
    if stats.hit_rate is None:
        return ["—"] * len(HIT_THRESHOLDS_F)
    return [f"{round(stats.hit_rate[t] * 100)}" for t in HIT_THRESHOLDS_F]


def _max_cell(stats: WindowStats) -> str:
    """max|err| и его дата: '5.2 08.07' или '—'."""
    if stats.max_abs_error is None or stats.max_abs_error_date is None:
        return "—"
    return f"{stats.max_abs_error:.1f} {stats.max_abs_error_date.strftime('%d.%m')}"


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    """Моноширинная таблица: ширина колонки — по максимуму содержимого, ячейки
    выравниваются по правому краю, разделитель — два пробела.
    """
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: list[str]) -> str:
        return "  ".join(c.rjust(widths[i]) for i, c in enumerate(cells))

    return "\n".join([fmt_row(headers), *(fmt_row(r) for r in rows)])


def format_model_errors(model: str, report: dict[str, WindowStats]) -> str:
    """Одна моноширинная таблица метрик для модели (все окна)."""
    headers = ["Окно", "N", "MAE", "ME", "RMSE", "≤1", "≤2", "≤3", "max|err|"]
    rows: list[list[str]] = []
    for w in WINDOWS:
        s = report[w]
        rows.append([
            WINDOW_LABELS[w],
            str(s.n),
            _cell(s.mae),
            _cell(s.bias, signed=True),
            _cell(s.rmse),
            *_hit_cells(s),
            _max_cell(s),
        ])
    table = _render_table(headers, rows)
    return f"<b>{model}</b>\n<pre>{html.escape(table)}</pre>"


def format_errors(reports: dict[str, dict[str, WindowStats]]) -> str:
    """Сводка ошибок по всем моделям.

    reports — {модель: {окно: WindowStats}} (из metrics.report на каждую модель).
    Каждая модель — своя таблица; внизу — легенда столбцов.
    """
    blocks = [format_model_errors(m, reports[m]) for m in reports]
    legend = (
        "MAE/ME/RMSE — °F; ME — систематическое смещение (прогноз−факт);\n"
        "≤1/≤2/≤3 — доля дней |err|≤N°F, %; N — дней с полными данными."
    )
    return "\n\n".join(blocks) + "\n\n" + legend


# --- /help -----------------------------------------------------------------

def _la_hm_to_msk(hour: int, minute: int, on: date) -> str:
    """Локальное время LA (hour:minute) -> 'HH:MM' в МСК на дату on.

    Смещение LA↔МСК зависит от летнего/зимнего времени в США (в РФ DST нет),
    поэтому переводим через реальную дату — так учитывается текущий сезон.
    """
    la_dt = datetime(on.year, on.month, on.day, hour, minute, tzinfo=LA)
    msk = la_dt.astimezone(MSK)
    return f"{msk.hour:02d}:{msk.minute:02d}"


def _schedule_line(hours: tuple[int, ...], minute: int, ref: date) -> str:
    """Строка вида '13:30, 19:30, 01:30, 07:30 МСК' для набора LA-часов."""
    times = ", ".join(_la_hm_to_msk(h, minute, ref) for h in sorted(hours))
    return f"{times} МСК"


def format_help(ref: date | None = None) -> str:
    """Подробная справка /help: модели, команды, метрики и расписание циклов.

    ref — дата, на которую считается перевод расписания в МСК (по умолчанию
    сегодня по LA): смещение LA↔МСК плавает с переходом на летнее время в США.
    """
    ref = ref or timeutil.la_today()
    fetch = _schedule_line(config.FETCH_HOURS_LA, config.FETCH_MINUTE, ref)
    verify = _schedule_line(config.VERIFY_HOURS_LA, config.VERIFY_MINUTE, ref)

    return (
        f"<b>Прогноз Tmax по станции {config.STATION} (Лос-Анджелес)</b>\n"
        "Максимальная температура за климатические сутки станции "
        "(зона America/Los_Angeles) и статистика ошибок моделей.\n\n"

        "<b>Модели</b>\n"
        "• <b>NBM</b> — National Blend of Models (бюллетень NBS, NOAA). "
        "Статистический ансамбль-блендинг множества моделей; основная модель.\n"
        "• <b>MAV</b> — GFS MOS (GFSMAV, NOAA/MDL). Статистическая коррекция "
        "выхода глобальной модели GFS под станцию.\n"
        "• <b>MET</b> — NAM MOS (NAMMET, NOAA/MDL). То же, но поверх модели NAM — "
        "независимый от GFS сигнал.\n"
        "Все дают Tmax на локальные сутки LA; берётся свежий выпуск (цикл 00/06/"
        "12/18Z — час выпуска в UTC).\n\n"

        "<b>Команды</b>\n"
        "• <b>/forecast</b> — прогноз Tmax на завтра по NBM, MAV и MET (°F и °C, "
        "с указанием цикла модели), плюс рынок Polymarket на те же сутки: "
        "вменённые вероятности диапазонов Tmax (цены Yes-долей), стрелка — "
        "диапазон, куда попал прогноз модели, и ссылка на ставку. Рынок "
        "резолвится по той же станции KLAX (Wunderground, целые °F).\n"
        "• <b>/errors</b> — метрики качества прогнозов по окнам (см. ниже).\n"
        "• <b>/help</b> — эта справка.\n"
        "• <b>/start</b> — краткая справка.\n\n"

        "<b>Метрики (/errors)</b>\n"
        "Для каждого дня «зачётный» прогноз сравнивается с фактом (Tmax из "
        "CLI-отчёта NWS, при отсутствии — расчёт по METAR). Ошибка дня = "
        "прогноз − факт (°F). Агрегаты по каждой модели:\n"
        "• <b>N</b> — число дней с полными данными (есть и прогноз, и факт).\n"
        "• <b>MAE</b> — средняя абсолютная ошибка, °F.\n"
        "• <b>ME</b> — систематическое смещение (среднее прогноз−факт), °F: "
        "«+» — модель завышает, «−» — занижает.\n"
        "• <b>RMSE</b> — среднеквадратичная ошибка, °F (сильнее штрафует "
        "крупные промахи).\n"
        "• <b>≤1 / ≤2 / ≤3</b> — доля дней с |ошибка| ≤ 1/2/3 °F, %.\n"
        "• <b>max|err|</b> — максимальная абсолютная ошибка за окно и её дата.\n"
        "Окна: <b>7д</b> и <b>30д</b> — скользящие; <b>сез</b> — с начала "
        "метеосезона (DJF/MAM/JJA/SON); <b>год</b> — с 1 января. Правый край "
        "всех окон — вчера (за сегодня факта ещё нет).\n\n"

        "<b>Циклы (расписание в МСК)</b>\n"
        f"• <b>Сбор прогнозов</b>: {fetch}. Берёт свежий доступный цикл NBM, "
        "MAV и MET (с лагом на публикацию бюллетеня) и пишет прогнозы в БД.\n"
        f"• <b>Сбор факта (верификация)</b>: {verify}. Забирает Tmax за вчера "
        "из CLI (фолбэк — METAR) для расчёта ошибок.\n"
        "Время LA→МСК: летом (амер. DST) +10 ч, зимой +11 ч — расписание выше "
        "уже пересчитано на текущий сезон."
    )
