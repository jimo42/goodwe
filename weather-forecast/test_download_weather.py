"""Hermetic tests for the standalone weather downloader."""

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("download-weather.py")
SPEC = importlib.util.spec_from_file_location("download_weather", MODULE_PATH)
download_weather = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(download_weather)


def test_forecast_horizon_is_three_full_days():
    assert download_weather.FORECAST_DAYS == 3
    assert download_weather.FORECAST_HOURS == 72


def test_split_by_day_preserves_all_nominal_hours():
    timestamps = [
        f"2026-08-{14 + day:02d}T{hour:02d}:00"
        for day in range(3)
        for hour in range(24)
    ]
    grouped = download_weather.split_by_day(
        timestamps,
        cloud=list(range(72)),
        sw=[0.0] * 72,
    )
    assert list(grouped) == ["2026-08-14", "2026-08-15", "2026-08-16"]
    assert [len(day["ts"]) for day in grouped.values()] == [24, 24, 24]
    assert sum(len(day["ts"]) for day in grouped.values()) == 72


if __name__ == "__main__":
    test_forecast_horizon_is_three_full_days()
    print("OK test_forecast_horizon_is_three_full_days")
    test_split_by_day_preserves_all_nominal_hours()
    print("OK test_split_by_day_preserves_all_nominal_hours")