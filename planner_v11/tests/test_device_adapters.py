"""Hermetic tests for v10 GoodWe/relay adapter helpers."""

import asyncio
import os
import struct
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import inverter_client, relay  # noqa: E402


def test_encode_schedule_matches_verified_12_byte_layout():
    raw = inverter_client.encode_schedule(13, 0, 13, 5, ["Sat"], -20, 90, True)
    assert len(raw) == 12
    assert struct.unpack(">BBBBbBhhh", raw) == (13, 0, 13, 5, -1, 64, -20, 90, 0)


def test_write_eco_mode_dry_run_does_not_connect():
    result = asyncio.run(
        inverter_client.write_eco_mode(1, 1, 0, 2, 0, inverter_client.ALL_DAYS, -20, 95, True, dry_run=True)
    )
    assert result["status"] == "dry_run_skipped"
    assert result["name"] == "eco_mode_1"
    assert result["requested"]["enabled"] is True


def test_write_eco_mode_verifies_on_off_with_fake_inverter():
    class FakeInverter:
        async def read_setting(self, name):
            return SimpleNamespace(on_off=-1)

        async def write_setting(self, name, raw):
            self.last_write = (name, raw)

    fake = FakeInverter()
    original_connect = inverter_client.connect

    async def fake_connect():
        return fake

    inverter_client.connect = fake_connect
    try:
        result = asyncio.run(
            inverter_client.write_eco_mode(2, 3, 0, 4, 0, ["Mon"], 50, 20, True, dry_run=False)
        )
    finally:
        inverter_client.connect = original_connect

    assert result["status"] == "written"


def test_write_eco_modes_dry_run_does_not_connect():
    schedules = [
        {
            "channel": i, "start_h": 10, "start_m": (i - 1) * 15,
            "end_h": 10 + (1 if i == 4 else 0), "end_m": (i * 15) % 60, "day_names": ["Sun"],
            "power_pct": 0, "soc_pct": 0, "enabled": True,
        }
        for i in range(1, 5)
    ]
    result = asyncio.run(inverter_client.write_eco_modes(schedules, dry_run=True))
    assert result["status"] == "dry_run_skipped"
    assert len(result["schedules"]) == 4


def test_parse_pole_uses_verified_bit_positions():
    # Positions are 1-indexed from left: 4=pump, 3=phase1, 2=phase2, 1=phase3.
    assert relay.parse_pole("1011") == {"pump": True, "phase1": True, "phase2": False, "phase3": True}
    assert relay.running_phase_count("1011") == 2


def test_set_phase_dry_run_reads_status_but_does_not_write():
    original_get_ip = relay._get_relay_ip
    original_http_get = relay._http_get
    calls = []
    relay._get_relay_ip = lambda: "192.0.2.10"
    relay._http_get = lambda path, ip, timeout=5.0: calls.append((path, ip)) or "0000"
    try:
        result = relay.set_phase("1", "ON", dry_run=True)
    finally:
        relay._get_relay_ip = original_get_ip
        relay._http_get = original_http_get

    assert result["status"] == "dry_run_skipped"
    assert calls == [("98", "192.0.2.10")]


def test_apply_phase_target_calls_only_required_phase_changes():
    original_read = relay.read_status_pole
    original_set = relay.set_phase
    actions = []
    relay.read_status_pole = lambda: "0000"

    def fake_set_phase(phase, state, *, dry_run=True, verify_delay_s=1.0):
        actions.append((phase, state, dry_run, verify_delay_s))
        return {"phase": phase, "state": state, "relay_status_ok": True, "status": "dry_run_skipped"}

    relay.set_phase = fake_set_phase
    try:
        result = relay.apply_phase_target(2, dry_run=True, verify_delay_s=2.0)
    finally:
        relay.read_status_pole = original_read
        relay.set_phase = original_set

    assert result["status"] == "written"
    assert result["relay_status_ok"] is True
    assert actions == [("1", "ON", True, 2.0), ("2", "ON", True, 2.0)]


def test_http_get_accepts_short_body_despite_incorrect_content_length(monkeypatch=None):
    class FakeSocket:
        def __init__(self):
            self.received = [
                b"HTTP/1.1 200 OK\r\nContent-Length: 16\r\n\r\n1011",
                b"",
            ]

        def settimeout(self, timeout):
            self.timeout = timeout

        def sendall(self, payload):
            self.payload = payload

        def recv(self, size):
            return self.received.pop(0)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    original = relay.socket.create_connection
    relay.socket.create_connection = lambda address, timeout: FakeSocket()
    try:
        result = relay._http_get("98", "192.0.2.10")
    finally:
        relay.socket.create_connection = original

    assert result == "1011"