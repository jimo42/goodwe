"""Testy pro lib/weather.py - hermetické, s vlastním temp adresářem CSV.

Spouštět na serveru: cd planner_v10 && python3 tests/run_manual.py
(pytest NENÍ na serveru nainstalován - viz HANDOFF_v10.md).
"""
import os
import shutil
import sys
import tempfile
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import weather as we  # noqa: E402

_TMP_DIR = None

_HEADER = "datetime;sun_pct;cloudcover_pct;shortwave_wm2;pv_estimate_kwh"


def _make_day_csv(path: str, day: date, rows: dict[int, tuple]) -> None:
    """rows: {hour: (sun_pct, cloudcover_pct, shortwave_wm2, pv_estimate_kwh)}"""
    lines = [_HEADER]
    for h in sorted(rows):
        sun_pct, cloud, sw, pv = rows[h]
        ts = f"{day.isoformat()}T{h:02d}:00"
        lines.append(f"{ts};{sun_pct};{cloud};{sw};{pv}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _setup_tmp_weather_dir() -> str:
    global _TMP_DIR
    _TMP_DIR = tempfile.mkdtemp(prefix="planner_v10_test_weather_")
    return _TMP_DIR


def _teardown_tmp_weather_dir() -> None:
    global _TMP_DIR
    if _TMP_DIR and os.path.isdir(_TMP_DIR):
        shutil.rmtree(_TMP_DIR, ignore_errors=True)
    _TMP_DIR = None


def test_parse_weather_csv_text_basic():
    text = (
        f"{_HEADER}\n"
        "2026-07-21T00:00;60;49;0.0;0.0\n"
        "2026-07-21T08:00;30;69;201.0;1.039\n"
        "malformed;row\n"
    )
    out = we._parse_weather_csv_text(text)
    assert "2026-07-21T00:00" in out
    assert out["2026-07-21T00:00"]["sun_pct"] == 60.0
    assert out["2026-07-21T08:00"]["pv_estimate_kwh"] == 1.039
    assert "malformed;row" not in out
    assert len(out) == 2


def test_read_weather_csv_missing_file_returns_empty():
    out = we.read_weather_csv("/nonexistent/weather/file.csv")
    assert out == {}


def test_load_day_weather_basic():
    d = _setup_tmp_weather_dir()
    try:
        day = date(2026, 7, 21)
        path = os.path.join(d, f"{day.isoformat()}.csv")
        _make_day_csv(path, day, {8: (30, 69, 201.0, 1.039), 12: (80, 10, 700.0, 4.5)})
        result = we.load_day_weather(day, weather_dir=d)
        assert len(result) == 2
        assert result[f"{day.isoformat()}T08:00"]["pv_estimate_kwh"] == 1.039
    finally:
        _teardown_tmp_weather_dir()


def test_load_day_weather_missing_day_returns_empty():
    d = _setup_tmp_weather_dir()
    try:
        result = we.load_day_weather(date(2026, 1, 1), weather_dir=d)
        assert result == {}
    finally:
        _teardown_tmp_weather_dir()


def test_get_weather_for_hour_rounds_down():
    d = _setup_tmp_weather_dir()
    try:
        day = date(2026, 7, 21)
        path = os.path.join(d, f"{day.isoformat()}.csv")
        _make_day_csv(path, day, {8: (30, 69, 201.0, 1.039)})
        dt = datetime(2026, 7, 21, 8, 47)
        rec = we.get_weather_for_hour(dt, weather_dir=d)
        assert rec is not None
        assert rec["pv_estimate_kwh"] == 1.039
    finally:
        _teardown_tmp_weather_dir()


def test_get_weather_for_hour_missing_returns_none():
    d = _setup_tmp_weather_dir()
    try:
        dt = datetime(2026, 1, 1, 8, 0)
        rec = we.get_weather_for_hour(dt, weather_dir=d)
        assert rec is None
    finally:
        _teardown_tmp_weather_dir()


def test_pv_estimate_kwh_for_slot_divides_evenly():
    d = _setup_tmp_weather_dir()
    try:
        day = date(2026, 7, 21)
        path = os.path.join(d, f"{day.isoformat()}.csv")
        _make_day_csv(path, day, {8: (30, 69, 201.0, 4.0)})
        dt = datetime(2026, 7, 21, 8, 10)
        val = we.pv_estimate_kwh_for_slot(dt, slot_minutes=15, weather_dir=d)
        assert val == 1.0  # 4.0 kWh/h * 15/60
    finally:
        _teardown_tmp_weather_dir()



def test_pv_estimate_kwh_for_slot_missing_returns_zero():
    d = _setup_tmp_weather_dir()
    try:
        dt = datetime(2026, 1, 1, 8, 0)
        val = we.pv_estimate_kwh_for_slot(dt, slot_minutes=15, weather_dir=d)
        assert val == 0.0
    finally:
        _teardown_tmp_weather_dir()


def test_sun_pct_and_cloudcover_for_slot():
    d = _setup_tmp_weather_dir()
    try:
        day = date(2026, 7, 21)
        path = os.path.join(d, f"{day.isoformat()}.csv")
        _make_day_csv(path, day, {8: (30, 69, 201.0, 1.039)})
        dt = datetime(2026, 7, 21, 8, 5)
        assert we.sun_pct_for_slot(dt, weather_dir=d) == 30.0
        assert we.cloudcover_pct_for_slot(dt, weather_dir=d) == 69.0
    finally:
        _teardown_tmp_weather_dir()


def test_sun_pct_for_slot_missing_returns_none():
    d = _setup_tmp_weather_dir()
    try:
        dt = datetime(2026, 1, 1, 8, 0)
        assert we.sun_pct_for_slot(dt, weather_dir=d) is None
        assert we.cloudcover_pct_for_slot(dt, weather_dir=d) is None
    finally:
        _teardown_tmp_weather_dir()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
