"""Testy pro lib/prices.py - hermetické, s vlastním temp adresářem CSV.

Spouštět na serveru: cd planner_v10 && python3 tests/run_manual.py
(pytest NENÍ na serveru nainstalován - viz HANDOFF_v10.md).
"""
import os
import shutil
import sys
import tempfile
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import prices as pr  # noqa: E402
from lib.config import load_config  # noqa: E402

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.toml")

_TMP_DIR = None


def _make_full_day_csv(path: str, base_price: float = 100.0) -> None:
    """Vytvoří kompletní 96-slotový den (HH:MM;cena), cena = base_price
    konstantně pro jednoduchost testů."""
    lines = []
    for h in range(24):
        for m in (0, 15, 30, 45):
            lines.append(f"{h:02d}:{m:02d};{base_price:.2f}".replace(".", ","))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _setup_tmp_prices_dir() -> str:
    global _TMP_DIR
    _TMP_DIR = tempfile.mkdtemp(prefix="planner_v10_test_prices_")
    return _TMP_DIR


def _teardown_tmp_prices_dir() -> None:
    global _TMP_DIR
    if _TMP_DIR and os.path.isdir(_TMP_DIR):
        shutil.rmtree(_TMP_DIR, ignore_errors=True)
    _TMP_DIR = None


def test_parse_price_csv_text_basic():
    text = "00:00;125,69\n00:15;115,29\n\ninvalid_line\n00:30;119,78\n"
    out = pr._parse_price_csv_text(text)
    assert out["00:00"] == 125.69
    assert out["00:15"] == 115.29
    assert out["00:30"] == 119.78
    assert "invalid_line" not in out
    assert len(out) == 3


def test_read_price_csv_missing_file_returns_empty():
    out = pr.read_price_csv("/nonexistent/path/does/not/exist.csv")
    assert out == {}


def test_load_actual_day_prices_complete_day():
    d = _setup_tmp_prices_dir()
    try:
        day = date(2026, 7, 21)
        path = os.path.join(d, f"{day.isoformat()}.csv")
        _make_full_day_csv(path, base_price=100.0)
        result = pr.load_actual_day_prices(day, prices_dir=d)
        assert result is not None
        assert len(result) == 96
        assert result["12:00"] == 100.0
    finally:
        _teardown_tmp_prices_dir()


def test_load_actual_day_prices_missing_returns_none():
    d = _setup_tmp_prices_dir()
    try:
        result = pr.load_actual_day_prices(date(2026, 1, 1), prices_dir=d)
        assert result is None
    finally:
        _teardown_tmp_prices_dir()


def test_load_actual_day_prices_fallback_to_check_csv():
    d = _setup_tmp_prices_dir()
    try:
        day = date(2026, 7, 21)
        # primary je nekompletní (jen 2 sloty)
        primary_path = os.path.join(d, f"{day.isoformat()}.csv")
        with open(primary_path, "w", encoding="utf-8") as f:
            f.write("00:00;100,00\n00:15;100,00\n")
        # _check je kompletní
        check_path = os.path.join(d, f"{day.isoformat()}_check.csv")
        _make_full_day_csv(check_path, base_price=200.0)
        result = pr.load_actual_day_prices(day, prices_dir=d)
        assert result is not None
        assert len(result) == 96
        assert result["12:00"] == 200.0
    finally:
        _teardown_tmp_prices_dir()


def test_get_actual_price_for_slot_rounds_down_to_quarter_hour():
    d = _setup_tmp_prices_dir()
    try:
        day = date(2026, 7, 21)
        path = os.path.join(d, f"{day.isoformat()}.csv")
        _make_full_day_csv(path, base_price=150.0)
        dt = datetime(2026, 7, 21, 14, 37)  # -> slot 14:30
        val = pr.get_actual_price_for_slot(dt, prices_dir=d)
        assert val == 150.0
    finally:
        _teardown_tmp_prices_dir()


def test_get_actual_price_for_slot_missing_day_returns_none():
    d = _setup_tmp_prices_dir()
    try:
        dt = datetime(2026, 1, 1, 12, 0)
        val = pr.get_actual_price_for_slot(dt, prices_dir=d)
        assert val is None
    finally:
        _teardown_tmp_prices_dir()


def test_historical_median_eur_mwh_basic():
    d = _setup_tmp_prices_dir()
    try:
        # tři kompletní dny s různými konstantními cenami -> medián = 200
        for i, price in enumerate([100.0, 200.0, 300.0], start=1):
            day = date(2026, 7, i)
            path = os.path.join(d, f"{day.isoformat()}.csv")
            _make_full_day_csv(path, base_price=price)
        median = pr.historical_median_eur_mwh(
            date(2026, 7, 5), prices_dir=d, lookback_days=10
        )
        assert median == 200.0
    finally:
        _teardown_tmp_prices_dir()


def test_historical_median_eur_mwh_no_data_returns_none():
    d = _setup_tmp_prices_dir()
    try:
        median = pr.historical_median_eur_mwh(date(2026, 1, 1), prices_dir=d)
        assert median is None
    finally:
        _teardown_tmp_prices_dir()


def test_fallback_estimate_eur_mwh_uses_config_margin():
    cfg = load_config(CONFIG_PATH)
    margin = cfg.economics.estimated_price_fallback_margin_eur_per_mwh
    assert margin == 30.00
    imp, exp = pr.fallback_estimate_eur_mwh(100.0, cfg)
    assert imp == 100.0 + margin
    assert exp == 100.0 - margin


def test_find_most_recent_complete_day():
    d = _setup_tmp_prices_dir()
    try:
        complete_day = date(2026, 7, 18)
        path = os.path.join(d, f"{complete_day.isoformat()}.csv")
        _make_full_day_csv(path, base_price=100.0)
        found = pr.find_most_recent_complete_day(date(2026, 7, 21), prices_dir=d)
        assert found == complete_day
    finally:
        _teardown_tmp_prices_dir()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
