"""PNG-графики метрик для Telegram-бота.

Рендеринг работает только с готовыми агрегатами ``app.metrics``: модуль не
обращается к БД и сети, а изображение возвращает как bytes без временных файлов.
Pillow выбран вместо matplotlib, чтобы не раздувать serverless-архив и холодный
старт функции ради нескольких простых столбчатых диаграмм.
"""

from __future__ import annotations

import math
from datetime import date
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from app import metrics
from app.metrics import WindowStats

Reports = dict[str, dict[str, WindowStats]]

WIDTH = 1400
HEIGHT = 1100

BACKGROUND = "#F4F7FB"
PANEL = "#FFFFFF"
INK = "#182230"
MUTED = "#667085"
GRID = "#DDE3EA"
ZERO = "#98A2B3"
MODEL_COLORS = {
    "NBM": "#2563EB",
    "MAV": "#F59E0B",
    "MET": "#10B981",
}
WINDOW_LABELS = {
    "7d": "7 days",
    "30d": "30 days",
    "season": "Season",
    "year": "Year",
}


def _font(size: int):
    """Масштабируемый встроенный шрифт Pillow без зависимости от ОС функции."""
    return ImageFont.load_default(size=size)


def _nice_ceiling(value: float) -> float:
    """Округлить верх шкалы вверх до удобного шага 1/2/5 × 10ⁿ."""
    if value <= 0:
        return 1.0
    power = 10 ** math.floor(math.log10(value))
    fraction = value / power
    nice = 1 if fraction <= 1 else 2 if fraction <= 2 else 5 if fraction <= 5 else 10
    return nice * power


def _metric_value(stats: WindowStats, field: str) -> float | None:
    if field == "hit2":
        if stats.hit_rate is None:
            return None
        value = stats.hit_rate.get(2.0)
        return None if value is None else value * 100
    value = getattr(stats, field)
    return None if value is None else float(value)


def _tick_label(value: float, percent: bool) -> str:
    if percent:
        return f"{value:.0f}%"
    if abs(value) < 0.05:
        value = 0.0
    return f"{value:.1f}"


def _draw_panel(
    draw: ImageDraw.ImageDraw,
    reports: Reports,
    models: tuple[str, ...],
    box: tuple[int, int, int, int],
    title: str,
    field: str,
    *,
    signed: bool = False,
    percent: bool = False,
) -> None:
    """Нарисовать одну сгруппированную столбчатую диаграмму."""
    left, top, right, bottom = box
    draw.rounded_rectangle(box, radius=24, fill=PANEL)
    draw.text((left + 28, top + 24), title, font=_font(28), fill=INK)

    plot_left = left + 82
    plot_right = right - 26
    plot_top = top + 72
    plot_bottom = bottom - 66
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    values = [
        _metric_value(reports[model][window], field)
        for window in metrics.WINDOWS
        for model in models
    ]
    present = [value for value in values if value is not None]
    if percent:
        y_min, y_max = 0.0, 100.0
    elif signed:
        extent = _nice_ceiling(max((abs(value) for value in present), default=1.0) * 1.15)
        y_min, y_max = -extent, extent
    else:
        y_min = 0.0
        y_max = _nice_ceiling(max(present, default=1.0) * 1.15)

    def y_pos(value: float) -> float:
        return plot_bottom - (value - y_min) / (y_max - y_min) * plot_height

    tick_count = 4
    for index in range(tick_count + 1):
        value = y_min + (y_max - y_min) * index / tick_count
        y = y_pos(value)
        color = ZERO if signed and abs(value) < 1e-9 else GRID
        width = 2 if signed and abs(value) < 1e-9 else 1
        draw.line((plot_left, y, plot_right, y), fill=color, width=width)
        draw.text(
            (plot_left - 12, y),
            _tick_label(value, percent),
            font=_font(17),
            fill=MUTED,
            anchor="rm",
        )

    zero_y = y_pos(0)
    group_width = plot_width / len(metrics.WINDOWS)
    gap = 7
    bar_width = min(34, int((group_width - 34) / max(len(models), 1) - gap))
    bar_width = max(bar_width, 14)
    bars_width = len(models) * bar_width + (len(models) - 1) * gap

    for window_index, window in enumerate(metrics.WINDOWS):
        center = plot_left + group_width * (window_index + 0.5)
        start_x = center - bars_width / 2
        for model_index, model in enumerate(models):
            value = _metric_value(reports[model][window], field)
            if value is None:
                continue
            x0 = start_x + model_index * (bar_width + gap)
            x1 = x0 + bar_width
            value_y = y_pos(value)
            if value >= 0:
                y0, y1 = min(value_y, zero_y - 2), zero_y
                label_y, anchor = y0 - 5, "mb"
            else:
                y0, y1 = zero_y, max(value_y, zero_y + 2)
                label_y, anchor = y1 + 5, "mt"
            draw.rounded_rectangle(
                (x0, y0, x1, y1),
                radius=4,
                fill=MODEL_COLORS.get(model, "#7C3AED"),
            )
            label = f"{value:.0f}" if percent else f"{value:.1f}"
            draw.text(
                ((x0 + x1) / 2, label_y),
                label,
                font=_font(15),
                fill=INK,
                anchor=anchor,
            )
        draw.text(
            (center, plot_bottom + 24),
            WINDOW_LABELS[window],
            font=_font(18),
            fill=MUTED,
            anchor="ma",
        )

    if not present:
        draw.text(
            ((plot_left + plot_right) / 2, (plot_top + plot_bottom) / 2),
            "No data",
            font=_font(24),
            fill=MUTED,
            anchor="mm",
        )


def _draw_legend(draw: ImageDraw.ImageDraw, models: tuple[str, ...]) -> None:
    item_width = 145
    total_width = item_width * len(models)
    x = WIDTH - 55 - total_width
    y = 112
    for model in models:
        draw.rounded_rectangle((x, y - 10, x + 28, y + 18), radius=5,
                               fill=MODEL_COLORS.get(model, "#7C3AED"))
        draw.text((x + 40, y + 4), model, font=_font(22), fill=INK, anchor="lm")
        x += item_width


def has_data(reports: Reports) -> bool:
    """Есть ли хотя бы один день с полными данными в любом окне/модели."""
    return any(stats.n > 0 for report in reports.values() for stats in report.values())


def render_metrics_chart(reports: Reports, ref: date) -> bytes:
    """Построить PNG-сравнение моделей по окнам, заканчивающимся вчера."""
    models = tuple(reports)
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)

    end = metrics.window_bounds(ref)["year"][1]
    draw.text((55, 48), "KLAX model performance", font=_font(42), fill=INK)
    draw.text(
        (55, 98),
        f"Windows end {end.isoformat()} (Los Angeles); temperature metrics in F",
        font=_font(22),
        fill=MUTED,
    )
    _draw_legend(draw, models)

    panels: tuple[tuple[tuple[int, int, int, int], str, str, bool, bool], ...] = (
        ((55, 155, 685, 555), "Mean absolute error (MAE)", "mae", False, False),
        ((715, 155, 1345, 555), "Root mean square error (RMSE)", "rmse", False, False),
        ((55, 585, 685, 985), "Bias / mean error (ME)", "bias", True, False),
        ((715, 585, 1345, 985), "Days within +/-2 F", "hit2", False, True),
    )
    for box, title, field, signed, percent in panels:
        _draw_panel(
            draw,
            reports,
            models,
            box,
            title,
            field,
            signed=signed,
            percent=percent,
        )

    draw.text(
        (WIDTH / 2, 1045),
        "Lower MAE/RMSE is better. Bias near zero is better. Exact N and max error: /errors",
        font=_font(20),
        fill=MUTED,
        anchor="mm",
    )

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
