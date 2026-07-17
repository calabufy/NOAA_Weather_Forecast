from datetime import date
from io import BytesIO

from PIL import Image

from app import metrics
from app.bot import charts
from app.metrics import WindowStats


def _reports() -> charts.Reports:
    reports = {}
    for model_index, model in enumerate(("NBM", "MAV", "MET")):
        report = {}
        for window_index, window in enumerate(metrics.WINDOWS):
            scale = 1 + model_index * 0.2 + window_index * 0.1
            report[window] = WindowStats(
                window=window,
                n=10 + window_index,
                mae=scale,
                bias=(-1) ** model_index * scale / 3,
                rmse=scale * 1.25,
                hit_rate={1.0: 0.5, 2.0: 0.75, 3.0: 0.9},
                max_abs_error=scale * 2,
                max_abs_error_date=date(2026, 7, 15),
            )
        reports[model] = report
    return reports


def test_render_metrics_chart_returns_valid_png():
    png = charts.render_metrics_chart(_reports(), date(2026, 7, 17))

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(BytesIO(png)) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.size == (charts.WIDTH, charts.HEIGHT)


def test_has_data_checks_window_sample_sizes():
    empty = {
        "NBM": {
            window: WindowStats(window=window, n=0) for window in metrics.WINDOWS
        }
    }

    assert charts.has_data(_reports())
    assert not charts.has_data(empty)
