"""Manuální spouštěč testů bez závislosti na pytestu.

Použití (na serveru): cd planner_v10 && python3 tests/run_manual.py
"""
import sys
import traceback
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tests.test_economics as t_economics  # noqa: E402
import tests.test_prices as t_prices  # noqa: E402
import tests.test_weather as t_weather  # noqa: E402
import tests.test_solver_adapter as t_solver_adapter  # noqa: E402
import tests.test_optimizer as t_optimizer  # noqa: E402
import tests.test_load_model as t_load_model  # noqa: E402
import tests.test_pool_model as t_pool_model  # noqa: E402
import tests.test_ev_model as t_ev_model  # noqa: E402
import tests.test_boiler_model as t_boiler_model  # noqa: E402
import tests.test_planner as t_planner  # noqa: E402
import tests.test_executor as t_executor  # noqa: E402
import tests.test_device_adapters as t_device_adapters  # noqa: E402
import tests.test_detectors as t_detectors  # noqa: E402
import tests.test_request_store as t_request_store  # noqa: E402
import tests.test_whatsapp_request_worker as t_whatsapp_request_worker  # noqa: E402
import tests.test_wallbox_client as t_wallbox_client  # noqa: E402
import tests.test_alerting as t_alerting  # noqa: E402
import tests.test_daily_report as t_daily_report  # noqa: E402
import tests.test_show_status as t_show_status  # noqa: E402
import tests.test_boiler_redesign as t_boiler_redesign  # noqa: E402

MODULES = [
    t_economics, t_prices, t_weather, t_solver_adapter, t_optimizer,
    t_load_model, t_pool_model, t_ev_model, t_boiler_model, t_planner,
    t_executor, t_device_adapters, t_detectors, t_request_store,
    t_whatsapp_request_worker, t_wallbox_client, t_alerting, t_daily_report,
    t_show_status, t_boiler_redesign,
]


total = 0
failed = 0
for mod in MODULES:
    names = [n for n in dir(mod) if n.startswith("test_")]
    print(f"--- {mod.__name__} ---")
    for n in names:
        total += 1
        try:
            getattr(mod, n)()
            print("OK  ", n)
        except Exception as e:
            failed += 1
            print("FAIL", n, "->", e)
            traceback.print_exc()

print()
print(f"{total - failed}/{total} passed")
sys.exit(1 if failed else 0)
