"""Hermetic tests for whatsapp_request_worker.py.

The tests use temporary spool directories and fake reply/status scripts, so no
real WhatsApp or production state paths are touched.
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import whatsapp_request_worker as worker  # noqa: E402


def _tmpdir():
    return tempfile.mkdtemp(prefix="planner_v10_test_whatsapp_worker_")


def _make_spool(root: Path) -> Path:
    spool = root / "spool"
    for name in ("incoming", "processing", "done", "failed"):
        (spool / name).mkdir(parents=True, exist_ok=True)
    return spool


def _write_request(spool: Path, filename: str, command: str, request_id: str = "req-1") -> None:
    payload = {
        "schema": 1,
        "request_id": request_id,
        "received_at": "2026-07-23T00:10:00+02:00",
        "source": {
            "type": "whatsapp",
            "session": "default",
            "chat_id": "group@g.us",
            "sender_id": "sender@lid",
            "message_id": f"msg-{request_id}",
        },
        "rule": "test",
        "commands": [command],
        "parameters": {},
    }
    with open(spool / "incoming" / filename, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _make_reply_script(root: Path) -> Path:
    script = root / "reply.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "out = pathlib.Path(sys.argv[1]).parent.parent / 'reply.log'\n"
        "text = sys.stdin.read()\n"
        "with open(out, 'a', encoding='utf-8') as f:\n"
        "    f.write(pathlib.Path(sys.argv[1]).name + '|' + text.replace('\\n', '\\u240a') + '\\n')\n"
        "print('sent')\n",
        encoding="utf-8",
    )
    os.chmod(script, 0o755)
    return script


def _make_status_script(root: Path) -> Path:
    script = root / "show_status.py"
    script.write_text("print('STATUS OK')\n", encoding="utf-8")
    return script


def _make_planner_script(root: Path, *, fail_first: bool = False, correlated: bool = True) -> Path:
    script = root / "planner.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "from datetime import datetime\n"
        f"root = pathlib.Path({str(root)!r})\n"
        "count_path = root / 'planner-count.txt'\n"
        "count = int(count_path.read_text() or '0') + 1 if count_path.exists() else 1\n"
        "count_path.write_text(str(count), encoding='utf-8')\n"
        f"if {fail_first!r} and count == 1:\n"
        "    raise SystemExit(3)\n"
        "request_doc = json.loads((root / 'requests.json').read_text(encoding='utf-8'))\n"
        "request = [item for item in request_doc['requests'] if item.get('status') == 'active'][-1]\n"
        f"request_id = request.get('id') if {correlated!r} else 'different-request'\n"
        "summary = {'id': request_id, 'type': request.get('type')}\n"
        "if request.get('type') == 'ev_charge':\n"
        "    summary.update({'required_ac_kwh': request.get('required_ac_kwh'), 'deadline': request.get('deadline'), "
        "'recommendation': {'feasible': True, 'recommended_start': '2026-07-23T01:00:00+02:00', "
        "'expected_end': '2026-07-23T05:00:00+02:00', 'latest_safe_start': '2026-07-23T04:15:00+02:00', "
        "'expected_delivered_kwh': request.get('required_ac_kwh'), 'reason': 'test'}})\n"
        "forecast = {'generated_at': datetime.now().astimezone().isoformat(), "
        "'solver': {'status': 'optimal'}, 'active_requests': [summary], "
        "'optimizer_slacks': {'ev_unserved_kwh': 0.0, 'boiler_hard_unserved_kwh': 0.0}, "
        "'slots': [{'slot_start': '2026-07-23T00:00:00+02:00'}, {'slot_start': '2026-07-23T13:00:00+02:00'}]}\n"
        "(root / 'forecast_48h.json').write_text(json.dumps(forecast), encoding='utf-8')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    os.chmod(script, 0o755)
    return script


def _paths(root: Path, spool: Path) -> worker.WorkerPaths:
    return worker.WorkerPaths(
        spool_dir=spool,
        requests_path=root / "requests.json",
        reply_script=_make_reply_script(root),
        show_status_script=_make_status_script(root),
        log_path=root / "worker.log",
        planner_script=root / "missing_planner.py",
        planner_log_path=root / "planner.log",
        forecast_path=root / "forecast_48h.json",
        async_status=False,
        async_planning=False,
    )


def _reply_log(spool: Path) -> str:
    path = spool / "reply.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_status_request_replies_with_show_status_output_and_moves_done():
    tmp = _tmpdir()
    try:
        root = Path(tmp)
        spool = _make_spool(root)
        _write_request(spool, "status.json", "status")
        assert worker.process_one(_paths(root, spool), verbose=False) is True
        assert (spool / "done" / "status.json").exists()
        assert "STATUS OK" in _reply_log(spool)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_status_request_defaults_to_async_deferred_processing():
    tmp = _tmpdir()
    original = worker.start_async_status_reply
    calls = []
    try:
        root = Path(tmp)
        spool = _make_spool(root)
        _write_request(spool, "status.json", "status")
        claimed = spool / "incoming" / "status.json"
        paths = worker.WorkerPaths(
            spool_dir=spool,
            requests_path=root / "requests.json",
            reply_script=_make_reply_script(root),
            show_status_script=_make_status_script(root),
            log_path=root / "worker.log",
            planner_script=root / "missing_planner.py",
            planner_log_path=root / "planner.log",
            forecast_path=root / "forecast_48h.json",
        )

        def fake_start(path, worker_paths, *, verbose=True):
            calls.append((path, worker_paths, verbose))

        worker.start_async_status_reply = fake_start
        outcome = worker.process_claimed(claimed, paths, verbose=False)
        assert outcome.deferred is True
        assert outcome.reply is None
        assert calls and calls[0][0] == claimed
    finally:
        worker.start_async_status_reply = original
        shutil.rmtree(tmp, ignore_errors=True)


def test_charge_car_request_is_stored_as_ev_charge():
    tmp = _tmpdir()
    try:
        root = Path(tmp)
        spool = _make_spool(root)
        paths = _paths(root, spool)
        _write_request(spool, "ev.json", "charge car;5.5kWh;2026-07-23T08:30:00+02:00")
        assert worker.process_one(paths, verbose=False) is True
        with open(paths.requests_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        item = doc["requests"][0]
        assert item["type"] == "ev_charge"
        assert item["required_ac_kwh"] == 5.5
        assert item["available_from"] == "2026-07-23T00:10:00+02:00"
        assert (spool / "done" / "ev.json").exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_heat_boiler_request_is_stored_as_boiler_full():
    tmp = _tmpdir()
    try:
        root = Path(tmp)
        spool = _make_spool(root)
        paths = _paths(root, spool)
        _write_request(spool, "boiler.json", "heat boiler;2026-07-23T08:30:00+02:00")
        assert worker.process_one(paths, verbose=False) is True
        with open(paths.requests_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["requests"][0]["type"] == "boiler_full"
        assert (spool / "done" / "boiler.json").exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_additional_load_request_is_stored_as_fixed_load():
    tmp = _tmpdir()
    try:
        root = Path(tmp)
        spool = _make_spool(root)
        paths = _paths(root, spool)
        _write_request(
            spool,
            "load.json",
            "additional load;2.5kW;2026-07-23T10:00:00+02:00;2026-07-23T12:30:00+02:00",
        )
        assert worker.process_one(paths, verbose=False) is True
        with open(paths.requests_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        item = doc["requests"][0]
        assert item["type"] == "additional_load"
        assert item["power_kw"] == 2.5
        assert item["phase"] is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_invalid_additional_load_moves_failed_and_replies_with_error():
    tmp = _tmpdir()
    try:
        root = Path(tmp)
        spool = _make_spool(root)
        paths = _paths(root, spool)
        _write_request(
            spool,
            "bad-load.json",
            "additional load;2.5kW;2026-07-23T12:30:00+02:00;2026-07-23T10:00:00+02:00",
        )
        assert worker.process_one(paths, verbose=False) is True
        assert (spool / "failed" / "bad-load.json").exists()
        assert "Konec dodatečné zátěže" in _reply_log(spool)
        assert not paths.requests_path.exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unknown_command_moves_failed_and_replies_with_error():
    tmp = _tmpdir()
    try:
        root = Path(tmp)
        spool = _make_spool(root)
        paths = _paths(root, spool)
        _write_request(spool, "unknown.json", "do something unexpected")
        assert worker.process_one(paths, verbose=False) is True
        assert (spool / "failed" / "unknown.json").exists()
        assert "Nerozumím internímu příkazu" in _reply_log(spool)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_requests_command_lists_numbered_active_requests():
    tmp = _tmpdir()
    try:
        root = Path(tmp)
        spool = _make_spool(root)
        paths = _paths(root, spool)
        paths.requests_path.write_text(
            json.dumps({
                "schema_version": 10,
                "requests": [
                    {
                        "id": "ev-1",
                        "type": "ev_charge",
                        "status": "active",
                        "required_ac_kwh": 5.0,
                        "deadline": "2099-07-24T20:00:00+02:00",
                    }
                ],
            }),
            encoding="utf-8",
        )
        paths.forecast_path.write_text(
            json.dumps({
                "active_requests": [
                    {
                        "id": "ev-1",
                        "type": "ev_charge",
                        "recommendation": {
                            "feasible": True,
                            "recommended_start": "2099-07-24T11:00:00+02:00",
                            "expected_end": "2099-07-24T14:45:00+02:00",
                        },
                    }
                ],
                "slots": [],
            }),
            encoding="utf-8",
        )
        _write_request(spool, "requests.json", "requests")
        assert worker.process_one(paths, verbose=False) is True
        reply = _reply_log(spool)
        assert "Aktivní požadavky" in reply
        assert "1. auto 5.0 kWh" in reply
        assert "Nabíjení naplánovat na" in reply
        assert "11:00–14:45" in reply
        assert ":00+02:00" not in reply
        assert "cancel 1" in reply
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cancel_future_request_marks_cancelled_and_replies_ok():
    tmp = _tmpdir()
    try:
        root = Path(tmp)
        spool = _make_spool(root)
        paths = _paths(root, spool)
        paths.requests_path.write_text(
            json.dumps({
                "schema_version": 10,
                "requests": [
                    {"id": "load-1", "type": "additional_load", "status": "active", "power_kw": 2.0}
                ],
            }),
            encoding="utf-8",
        )
        _write_request(spool, "cancel.json", "cancel 1")
        assert worker.process_one(paths, verbose=False) is True
        with open(paths.requests_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["requests"][0]["status"] == "cancelled"
        assert "cancel.json|ok" in _reply_log(spool)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cancel_running_additional_load_mentions_running():
    tmp = _tmpdir()
    try:
        root = Path(tmp)
        spool = _make_spool(root)
        paths = _paths(root, spool)
        paths.requests_path.write_text(
            json.dumps({
                "schema_version": 10,
                "requests": [
                    {
                        "id": "load-1",
                        "type": "additional_load",
                        "status": "active",
                        "power_kw": 2.0,
                        "start": "2020-01-01T00:00:00+02:00",
                        "end": "2099-01-01T00:00:00+02:00",
                    }
                ],
            }),
            encoding="utf-8",
        )
        _write_request(spool, "cancel-running.json", "cancel 1")
        assert worker.process_one(paths, verbose=False) is True
        assert "právě probíhá" in _reply_log(spool)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_quiet_no_request_does_not_write_log_noise():
    tmp = _tmpdir()
    try:
        root = Path(tmp)
        spool = _make_spool(root)
        paths = _paths(root, spool)
        assert worker.process_one(paths, verbose=False) is False
        assert not paths.log_path.exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_user_datetime_format_includes_weekday_and_omits_seconds_timezone():
    text = worker.format_user_datetime("2026-07-23T08:30:45+02:00")
    assert text == "čtvrtek 23. 7. 2026 08:30"
    assert ":45" not in text
    assert "+02:00" not in text


def test_ev_final_confirmation_contains_single_recommended_timeframe():
    request = {
        "id": "ev-1",
        "type": "ev_charge",
        "required_ac_kwh": 10.0,
        "deadline": "2026-07-23T08:30:00+02:00",
    }
    forecast = {
        "solver": {"status": "optimal"},
        "active_requests": [{
            "id": "ev-1",
            "type": "ev_charge",
            "recommendation": {
                "feasible": True,
                "recommended_start": "2026-07-23T01:00:00+02:00",
                "expected_end": "2026-07-23T05:00:00+02:00",
                "latest_safe_start": "2026-07-23T04:15:00+02:00",
            },
        }],
    }
    reply = worker.request_planning_reply(request, forecast)
    assert "Nabíjení nastavte na čtvrtek 23. 7. 2026 01:00–05:00" in reply
    assert "Nejpozdější bezpečný start je čtvrtek 23. 7. 2026 04:15" in reply
    assert reply.count("Nabíjení nastavte na") == 1
    assert "+02:00" not in reply


def test_boiler_and_additional_load_final_confirmations_come_from_fresh_plan():
    boiler = {
        "id": "boiler-1",
        "type": "boiler_full",
        "deadline": "2026-07-23T08:30:00+02:00",
    }
    boiler_forecast = {
        "solver": {"status": "optimal"},
        "active_requests": [{"id": "boiler-1", "type": "boiler_full"}],
        "optimizer_slacks": {"boiler_hard_unserved_kwh": 0.0},
    }
    assert "je zahrnutý v novém plánu" in worker.request_planning_reply(boiler, boiler_forecast)

    load = {
        "id": "load-1",
        "type": "additional_load",
        "power_kw": 2.5,
        "start": "2026-07-23T10:00:00+02:00",
        "end": "2026-07-23T12:30:00+02:00",
    }
    load_forecast = {
        "solver": {"status": "optimal"},
        "active_requests": [{"id": "load-1", "type": "additional_load"}],
        "slots": [
            {"slot_start": "2026-07-23T00:00:00+02:00"},
            {"slot_start": "2026-07-23T23:45:00+02:00"},
        ],
    }
    reply = worker.request_planning_reply(load, load_forecast)
    assert "je zahrnutá v novém plánu" in reply
    assert "10:00–12:30" in reply


def test_replan_retries_transient_failure_and_requires_request_correlation():
    tmp = _tmpdir()
    try:
        root = Path(tmp)
        spool = _make_spool(root)
        paths = worker.WorkerPaths(
            spool_dir=spool,
            requests_path=root / "requests.json",
            reply_script=_make_reply_script(root),
            show_status_script=_make_status_script(root),
            log_path=root / "worker.log",
            planner_script=_make_planner_script(root, fail_first=True),
            planner_log_path=root / "planner.log",
            forecast_path=root / "forecast_48h.json",
            async_status=False,
            async_planning=False,
        )
        paths.requests_path.write_text(json.dumps({
            "requests": [{
                "id": "ev-1",
                "type": "ev_charge",
                "status": "active",
                "required_ac_kwh": 10.0,
                "deadline": "2026-07-23T08:30:00+02:00",
            }]
        }), encoding="utf-8")
        result = worker.run_planner_replan(
            paths,
            request_id="ev-1",
            not_before=datetime.now().astimezone() - timedelta(seconds=1),
            verbose=False,
            max_attempts=2,
            retry_seconds=0,
        )
        assert result is not None
        assert (root / "planner-count.txt").read_text(encoding="utf-8") == "2"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_replan_rejects_fresh_but_uncorrelated_forecast():
    tmp = _tmpdir()
    try:
        root = Path(tmp)
        spool = _make_spool(root)
        paths = worker.WorkerPaths(
            spool_dir=spool,
            requests_path=root / "requests.json",
            reply_script=_make_reply_script(root),
            show_status_script=_make_status_script(root),
            log_path=root / "worker.log",
            planner_script=_make_planner_script(root, correlated=False),
            planner_log_path=root / "planner.log",
            forecast_path=root / "forecast_48h.json",
            async_status=False,
            async_planning=False,
        )
        paths.requests_path.write_text(json.dumps({
            "requests": [{
                "id": "ev-1",
                "type": "ev_charge",
                "status": "active",
                "required_ac_kwh": 10.0,
                "deadline": "2026-07-23T08:30:00+02:00",
            }]
        }), encoding="utf-8")
        result = worker.run_planner_replan(
            paths,
            request_id="ev-1",
            not_before=datetime.now().astimezone() - timedelta(seconds=1),
            verbose=False,
            max_attempts=1,
            retry_seconds=0,
        )
        assert result is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_new_request_gets_immediate_ack_and_defers_final_planning():
    tmp = _tmpdir()
    original = worker.start_async_request_planning
    calls = []
    try:
        root = Path(tmp)
        spool = _make_spool(root)
        paths = worker.WorkerPaths(
            spool_dir=spool,
            requests_path=root / "requests.json",
            reply_script=_make_reply_script(root),
            show_status_script=_make_status_script(root),
            log_path=root / "worker.log",
            planner_script=root / "planner.py",
            planner_log_path=root / "planner.log",
            forecast_path=root / "forecast_48h.json",
            async_status=False,
            async_planning=True,
        )
        _write_request(spool, "ev-async.json", "charge car;10kWh;2026-07-23T08:30:00+02:00", "ev-async")

        def fake_start(path, worker_paths, *, not_before, verbose=True):
            calls.append((path, worker_paths, not_before, verbose))

        worker.start_async_request_planning = fake_start
        assert worker.process_one(paths, verbose=False) is True
        assert (spool / "processing" / "ev-async.json").exists()
        assert not (spool / "done" / "ev-async.json").exists()
        assert "Přepočítávám plán" in _reply_log(spool)
        assert len(calls) == 1
    finally:
        worker.start_async_request_planning = original
        shutil.rmtree(tmp, ignore_errors=True)