"""Hermetic tests for lib.wallbox_client."""

import json
import tempfile
from pathlib import Path

from lib import wallbox_client


def test_parse_chargedata_extracts_power_w_and_energy_kwh():
    power_w, energy_kwh = wallbox_client.parse_chargedata("4327|3069|2.95|")
    assert power_w == 3069.0
    assert energy_kwh == 2.95


def test_parse_wallbox_payload_uses_chargedata_power():
    state = wallbox_client.parse_wallbox_payload({
        "secc": {
            "port0": {
                "salia": {"chargedata": "4327|3069|2.95|"},
                "metering": {
                    "current": {"ac": {"l1": {"actual": "13227"}}},
                    "voltage": {"ac": {"l1": {"actual": "23194"}}},
                },
            }
        }
    })
    assert state.available is True
    assert state.charging_power_w == 3069.0
    assert state.charging_power_kw == 3.069
    assert state.charging_energy_kwh == 2.95
    assert state.source == "salia.chargedata"


def test_parse_wallbox_payload_falls_back_to_l1_current_voltage():
    state = wallbox_client.parse_wallbox_payload({
        "secc": {
            "port0": {
                "metering": {
                    "current": {"ac": {"l1": {"actual": "13227"}}},
                    "voltage": {"ac": {"l1": {"actual": "23194"}}},
                }
            }
        }
    })
    assert round(state.charging_power_w or 0.0) == 3068
    assert state.source == "l1_current_voltage"


def test_read_wallbox_state_returns_unavailable_on_bad_json():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"not-json"

    def opener(url, timeout):
        return Response()

    state = wallbox_client.read_wallbox_state(
        url="http://example.invalid/api/", opener=opener, attempts=1
    )
    assert state.available is False
    assert state.error


def test_read_wallbox_state_returns_unavailable_without_configured_url():
    state = wallbox_client.read_wallbox_state(url="", attempts=1)
    assert state.available is False
    assert state.source == "unavailable"
    assert state.error == "wallbox API URL is not configured"


def test_read_wallbox_state_uses_injected_opener():
    payload = {"secc": {"port0": {"salia": {"chargedata": "4327|3069|2.95|"}}}}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    def opener(url, timeout):
        assert url == "http://example.invalid/api/"
        assert timeout == 1.5
        return Response()

    state = wallbox_client.read_wallbox_state(url="http://example.invalid/api/", timeout_seconds=1.5, opener=opener)
    assert state.available is True
    assert state.charging_power_w == 3069.0


def test_read_wallbox_state_retries_transient_errors_without_sleep():
    calls = []
    payload = {"secc": {"port0": {"salia": {"chargedata": "18159|3060|6.89||"}}}}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    def opener(url, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise OSError("temporary")
        return Response()

    state = wallbox_client.read_wallbox_state(
        url="http://example.invalid/api/",
        opener=opener,
        attempts=5,
        retry_delay_seconds=0,
    )
    assert state.available is True
    assert len(calls) == 3
    assert state.charging_energy_kwh == 6.89


def test_private_url_can_be_loaded_from_untracked_conf():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wallbox.conf"
        path.write_text("[wallbox]\napi_url = http://example.invalid/api/\n", encoding="utf-8")
        assert wallbox_client.resolve_wallbox_api_url(None, environ={}, config_path=path).endswith("/api/")