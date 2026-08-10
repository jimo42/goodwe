from datetime import datetime, timedelta
from pathlib import Path
import shutil
import tempfile
from zoneinfo import ZoneInfo

from lib import ev_session


TZ = ZoneInfo("Europe/Prague")


def test_completion_notification_marker_preserves_replan_claim():
    now = datetime(2026, 8, 10, 17, 0, tzinfo=TZ)
    tmp = Path(tempfile.mkdtemp(prefix="planner_v11_ev_marker_"))
    path = tmp / "ev.json"
    try:
        ev_session.write_state(path, {
            "session_id": "ev-current", "state": "CLOSED",
            "replan_required": False, "replan_claimed_at": now.isoformat(),
        })
        marked = ev_session.mark_completion_notification_sent(
            path, session_id="ev-current", now=now,
        )
        assert marked["replan_claimed_at"] == now.isoformat()
        assert marked["completion_notification_sent_at"] == now.isoformat(timespec="seconds")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _wallbox(power_w: float, energy_kwh: float) -> dict:
    return {
        "available": True,
        "charging_power_w": power_w,
        "charging_energy_kwh": energy_kwh,
        "source": "salia.chargedata",
        "error": None,
    }


def test_synthetic_session_uses_direct_counter_and_six_kwh_target():
    now = datetime(2026, 8, 7, 12, 0, tzinfo=TZ)
    state = ev_session.update_session({}, now=now, wallbox=_wallbox(3060, 6.89))
    assert state["state"] == "ACTIVE"
    assert state["request_source"] == "synthetic"
    assert state["effective_target_kwh"] == 6.0
    assert state["delivered_kwh"] == 6.89
    assert state["physical_remaining_to_max_kwh"] == 2.11


def test_first_low_power_sample_with_counter_bootstraps_paused_session():
    now = datetime(2026, 8, 7, 18, 0, tzinfo=TZ)
    request = {"id": "ev-1", "required_ac_kwh": 8.0}
    state = ev_session.update_session({}, now=now, wallbox=_wallbox(6, 7.9), active_ev_request=request)
    assert state["state"] == "PAUSED"
    assert state["request_id"] == "ev-1"
    assert state["delivered_kwh"] == 7.9
    assert state["bootstrap_from_low_power"] is True
    assert state["replan_reason"] == "EV_SESSION_DISCOVERED_PAUSED"


def test_user_target_is_capped_and_original_is_audited():
    now = datetime(2026, 8, 7, 12, 0, tzinfo=TZ)
    request = {"id": "ev-1", "required_ac_kwh": 12.0}
    state = ev_session.update_session({}, now=now, wallbox=_wallbox(1000, 0.5), active_ev_request=request)
    assert state["requested_ac_kwh_original"] == 12.0
    assert state["effective_target_kwh"] == 9.0


def test_exactly_thirty_minutes_is_same_session_and_resume_keeps_id():
    start = datetime(2026, 8, 7, 12, 0, tzinfo=TZ)
    active = ev_session.update_session({}, now=start, wallbox=_wallbox(3060, 1.0))
    paused = ev_session.update_session(active, now=start + timedelta(minutes=5), wallbox=_wallbox(7, 1.1))
    exact = ev_session.update_session(paused, now=start + timedelta(minutes=35), wallbox=_wallbox(6, 1.1))
    resumed = ev_session.update_session(exact, now=start + timedelta(minutes=36), wallbox=_wallbox(2500, 1.2))
    assert exact["state"] == "PAUSED"
    assert resumed["state"] == "ACTIVE"
    assert resumed["session_id"] == active["session_id"]


def test_more_than_thirty_minutes_closes_and_large_deviation_replans():
    start = datetime(2026, 8, 7, 12, 0, tzinfo=TZ)
    request = {"id": "ev-1", "required_ac_kwh": 8.0}
    active = ev_session.update_session({}, now=start, wallbox=_wallbox(3060, 3.0), active_ev_request=request)
    paused = ev_session.update_session(active, now=start + timedelta(minutes=5), wallbox=_wallbox(7, 3.0))
    closed = ev_session.update_session(paused, now=start + timedelta(minutes=36), wallbox=_wallbox(7, 3.0))
    assert closed["state"] == "CLOSED"
    assert closed["final_deviation_kwh"] == -5.0
    assert closed["replan_reason"] == "EV_SESSION_CLOSED_DEVIATION"


def test_api_outage_preserves_pause_clock_and_energy():
    start = datetime(2026, 8, 7, 12, 0, tzinfo=TZ)
    active = ev_session.update_session({}, now=start, wallbox=_wallbox(3060, 2.0))
    paused = ev_session.update_session(active, now=start + timedelta(minutes=5), wallbox=_wallbox(5, 2.1))
    unavailable = ev_session.update_session(
        paused,
        now=start + timedelta(hours=2),
        wallbox={"available": False, "source": "unavailable", "error": "timeout"},
    )
    assert unavailable["state"] == "PAUSED"
    assert unavailable["low_power_since"] == paused["low_power_since"]
    assert unavailable["delivered_kwh"] == paused["delivered_kwh"]


def test_replan_claim_is_idempotent():
    now = datetime(2026, 8, 7, 12, 0, tzinfo=TZ)
    tmp = Path(tempfile.mkdtemp(prefix="planner_v11_ev_session_"))
    try:
        path = tmp / "ev_session.json"
        ev_session.write_state(path, {"replan_required": True, "replan_reason": "EV_SESSION_STARTED"})
        first, _ = ev_session.claim_replan(path, now=now)
        second, state = ev_session.claim_replan(path, now=now + timedelta(minutes=5))
        assert first is True
        assert second is False
        assert state["last_replan_reason"] == "EV_SESSION_STARTED"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
