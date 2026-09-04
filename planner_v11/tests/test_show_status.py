"""Hermetic unit tests for show_status.py formatting helpers."""
import importlib.util
import os
import sys
from pathlib import Path


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

STATUS_PATH = Path(__file__).resolve().parent.parent / "show_status.py"
spec = importlib.util.spec_from_file_location("show_status_v10_module", STATUS_PATH)
show_status = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = show_status
spec.loader.exec_module(show_status)


def test_fmt_grid_flow_distinguishes_import_export_and_unavailable():
    assert show_status.fmt_grid_flow(5000) == "export 5000 W"
    assert show_status.fmt_grid_flow(-5000) == "import 5000 W"
    assert show_status.fmt_grid_flow(0) == "0 W (bez toku)"
    assert show_status.fmt_grid_flow(None) == "n/a"


def test_status_formats_current_pv_power_and_export_permission():
    assert show_status.fmt_pv_power({"ppv1": 1200, "ppv2": 800}) == "2000 W"
    assert show_status.fmt_export_permission({"zero_export_active": True}) == "zakázáno"
    assert show_status.fmt_export_permission({"zero_export_active": False}) == "povoleno"


def test_show_status_identifies_v11():
    assert "planner_v11" in show_status.__doc__
    assert show_status.VERSION == "1.3"
