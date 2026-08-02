"""Read-only wallbox API client for immediate EV charging detection.

VERSION = "1.1"

Changelog:
- v1.1 (2026-08-03): Remove hardcoded local wallbox URL from source; production
  URL must be supplied by ignored local configuration.
- v1.0 (2026-07-24): Add stdlib-only reader for the wallbox JSON API.

The API contract was verified by the operator from shell examples:
`/api/ -> secc.port0.salia.chargedata == "4327|3069|2.95|"`, where the
second field is current charging power in W and the third field is charging
energy in kWh since the session start. As a fallback, L1 current in mA and L1
voltage in centivolts can be multiplied to estimate W.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


VERSION = "1.1"

DEFAULT_WALLBOX_API_URL = ""
DEFAULT_TIMEOUT_SECONDS = 2.0


class WallboxReadError(Exception):
    """Raised when wallbox API data cannot be read or parsed."""


@dataclass(frozen=True)
class WallboxState:
    available: bool
    charging_power_w: float | None
    charging_energy_kwh: float | None
    l1_current_ma: float | None
    l1_voltage_cv: float | None
    source: str
    error: str | None = None

    @property
    def charging_power_kw(self) -> float | None:
        if self.charging_power_w is None:
            return None
        return max(0.0, self.charging_power_w / 1000.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "charging_power_w": None if self.charging_power_w is None else round(self.charging_power_w, 1),
            "charging_power_kw": None if self.charging_power_kw is None else round(self.charging_power_kw, 3),
            "charging_energy_kwh": None if self.charging_energy_kwh is None else round(self.charging_energy_kwh, 3),
            "l1_current_ma": None if self.l1_current_ma is None else round(self.l1_current_ma, 1),
            "l1_voltage_cv": None if self.l1_voltage_cv is None else round(self.l1_voltage_cv, 1),
            "source": self.source,
            "error": self.error,
        }


def _nested_get(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def parse_chargedata(value: Any) -> tuple[float | None, float | None]:
    """Parse wallbox `salia.chargedata` as power W and energy kWh.

    Expected string example: `"4327|3069|2.95|"`.
    """

    if not isinstance(value, str):
        return None, None
    parts = value.split("|")
    if len(parts) < 3:
        return None, None
    power_w = _float_or_none(parts[1])
    energy_kwh = _float_or_none(parts[2])
    return power_w, energy_kwh


def parse_wallbox_payload(payload: dict[str, Any]) -> WallboxState:
    """Extract a read-only wallbox state from the JSON payload."""

    chargedata = _nested_get(payload, ("secc", "port0", "salia", "chargedata"))
    power_w, energy_kwh = parse_chargedata(chargedata)

    current_ma = _float_or_none(_nested_get(payload, ("secc", "port0", "metering", "current", "ac", "l1", "actual")))
    voltage_cv = _float_or_none(_nested_get(payload, ("secc", "port0", "metering", "voltage", "ac", "l1", "actual")))

    source = "salia.chargedata"
    if power_w is None and current_ma is not None and voltage_cv is not None:
        # mA * centivolt -> (A * V) = W, with divisors 1000 and 100.
        power_w = (current_ma / 1000.0) * (voltage_cv / 100.0)
        source = "l1_current_voltage"

    if power_w is None and energy_kwh is None and current_ma is None and voltage_cv is None:
        raise WallboxReadError("wallbox payload does not contain supported charging fields")

    return WallboxState(
        available=True,
        charging_power_w=power_w,
        charging_energy_kwh=energy_kwh,
        l1_current_ma=current_ma,
        l1_voltage_cv=voltage_cv,
        source=source,
    )


def read_wallbox_state(
    *,
    url: str = DEFAULT_WALLBOX_API_URL,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., Any] | None = None,
) -> WallboxState:
    """Read wallbox API using only Python stdlib.

    The function returns an unavailable state instead of raising for network or
    parse errors, so executor can safely fall back to phase-load heuristics.
    """

    if not url:
        return WallboxState(
            available=False,
            charging_power_w=None,
            charging_energy_kwh=None,
            l1_current_ma=None,
            l1_voltage_cv=None,
            source="unavailable",
            error="wallbox API URL is not configured",
        )

    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(url, timeout=timeout_seconds) as response:
            raw = response.read()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise WallboxReadError("wallbox API returned non-object JSON")
        return parse_wallbox_payload(payload)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, WallboxReadError) as exc:
        return WallboxState(
            available=False,
            charging_power_w=None,
            charging_energy_kwh=None,
            l1_current_ma=None,
            l1_voltage_cv=None,
            source="unavailable",
            error=str(exc),
        )