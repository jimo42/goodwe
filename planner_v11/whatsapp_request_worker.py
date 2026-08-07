#!/usr/bin/env python3
"""Process validated WhatsApp requests from the spool into planner_v11.

VERSION = "1.6"

Changelog:
- v1.6 (2026-08-07): Cap one EV request at 9 kWh while retaining the original
  requested energy for audit and explicitly informing the user.
- v1.5 (2026-08-04): Persist the initial EV schedule notification baseline
  only after the correlated final WhatsApp reply succeeds.
- v1.4 (2026-08-03): Wait for a fresh correlated on-demand planner result for
  EV, boiler and additional-load requests, retry transient planner failures,
  return final planning confirmations, and format user-facing times without
  seconds/timezone. Include the current EV recommendation in `requests`.
- v1.3 (2026-07-27): Expire past requests before listing/cancellation and
  return the completed EV recommendation after an on-demand replan.
- v1.2 (2026-07-25): Reduce `--quiet` polling log noise; do not append
  no-request and zero-processed heartbeat lines every 10 seconds/minute.
- v1.1 (2026-07-24): Add `requests`/`cancel N`, async planner replan trigger
  after request changes, asynchronous status replies, and cron-friendly polling loop.
- v1.0 (2026-07-23): One-shot spool worker for status, EV, boiler and
  announced additional load requests.

Contract source: WHATSAPP_AUTOMATION.md / analysis_results/AUTOMATION.md.
This worker never evaluates free-form user text. It accepts only validated
internal commands written by the WhatsApp wrapper into `incoming/*.json`.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from lib import request_store


MAX_EV_SESSION_KWH = 9.0


VERSION = "1.6"

PLANNER_REPLAN_MAX_ATTEMPTS = 3
PLANNER_REPLAN_RETRY_SECONDS = 5.0

PLANNER_DIR = Path(__file__).resolve().parent
STATE_DIR = PLANNER_DIR / "state"
DEFAULT_REQUESTS_PATH = STATE_DIR / "requests.json"
DEFAULT_SPOOL_DIR = Path("/home/automatization/goodwe/whatsapp-spool")
DEFAULT_REPLY_SCRIPT = Path("/home/automatization/goodwe/bin/reply_whatsapp.sh")
DEFAULT_SHOW_STATUS = PLANNER_DIR / "show_status.py"
DEFAULT_LOG_PATH = Path("/home/automatization/goodwe/logs/whatsapp_request_worker.log")
DEFAULT_PLANNER_SCRIPT = PLANNER_DIR / "planner.py"
DEFAULT_PLANNER_LOG_PATH = Path("/home/automatization/goodwe/logs/planner_v11.log")
DEFAULT_FORECAST_PATH = STATE_DIR / "forecast_48h.json"

CHARGE_CAR_RE = re.compile(r"^charge car;(?P<energy>[0-9]+(?:[.,][0-9]+)?)kWh;(?P<deadline>.+)$")
HEAT_BOILER_RE = re.compile(r"^heat boiler;(?P<deadline>.+)$")
ADDITIONAL_LOAD_RE = re.compile(
    r"^additional load;(?P<power>[0-9]+(?:[.,][0-9]+)?)kW;(?P<start>.+);(?P<end>.+)$"
)
CANCEL_RE = re.compile(r"^cancel\s+(?P<display_id>[0-9]+)$")


class RequestError(Exception):
    """User-visible request validation or command error."""


@dataclass(frozen=True)
class WorkerPaths:
    spool_dir: Path = DEFAULT_SPOOL_DIR
    requests_path: Path = DEFAULT_REQUESTS_PATH
    reply_script: Path = DEFAULT_REPLY_SCRIPT
    show_status_script: Path = DEFAULT_SHOW_STATUS
    log_path: Path = DEFAULT_LOG_PATH
    planner_script: Path = DEFAULT_PLANNER_SCRIPT
    planner_log_path: Path = DEFAULT_PLANNER_LOG_PATH
    forecast_path: Path = DEFAULT_FORECAST_PATH
    async_status: bool = True
    async_planning: bool = True


@dataclass(frozen=True)
class ProcessOutcome:
    success: bool
    reply: str | None
    deferred: bool = False
    planning_request: dict[str, Any] | None = None
    planning_forecast: dict[str, Any] | None = None


def log(message: str, paths: WorkerPaths, *, verbose: bool = True) -> None:
    line = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] whatsapp_worker v{VERSION}: {message}"
    if verbose:
        print(line)
    try:
        paths.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(paths.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def parse_abs_iso(value: str, field_name: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RequestError(f"Neplatný čas v poli {field_name}: {value!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise RequestError(f"Čas v poli {field_name} musí být absolutní ISO-8601 s časovým posunem.")
    return dt.isoformat(timespec="seconds")


def parse_float(value: str, field_name: str) -> float:
    try:
        parsed = float(value.replace(",", "."))
    except ValueError as exc:
        raise RequestError(f"Neplatné číslo v poli {field_name}: {value!r}") from exc
    if parsed <= 0:
        raise RequestError(f"Hodnota {field_name} musí být větší než nula.")
    return parsed


def validate_envelope(payload: Any) -> tuple[str, str, str, list[str], str]:
    if not isinstance(payload, dict):
        raise RequestError("Request není JSON objekt.")
    if payload.get("schema") != 1:
        raise RequestError("Nepodporované schema requestu.")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise RequestError("Request nemá platné request_id.")
    source = payload.get("source")
    if not isinstance(source, dict):
        raise RequestError("Request nemá platný source objekt.")
    message_id = source.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise RequestError("Request nemá platné source.message_id.")
    sender_id = source.get("sender_id")
    if not isinstance(sender_id, str) or not sender_id:
        raise RequestError("Request nemá platné source.sender_id.")
    commands = payload.get("commands")
    if not isinstance(commands, list) or not commands or not all(isinstance(c, str) for c in commands):
        raise RequestError("Request nemá platný seznam commands.")
    if len(commands) != 1:
        raise RequestError("Request musí obsahovat právě jeden interní příkaz.")
    received_at = payload.get("received_at")
    created_at = received_at if isinstance(received_at, str) and received_at else datetime.now().astimezone().isoformat(timespec="seconds")
    return request_id, message_id, sender_id, commands, created_at


def load_request(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise RequestError(f"Request není validní JSON: {exc}") from exc


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def claim_one(spool_dir: Path) -> Path | None:
    incoming = spool_dir / "incoming"
    processing = spool_dir / "processing"
    processing.mkdir(parents=True, exist_ok=True)
    for source in sorted(incoming.glob("*.json")):
        claimed = processing / source.name
        try:
            source.replace(claimed)
        except OSError:
            continue
        return claimed
    return None


def move_request(path: Path, spool_dir: Path, target: str) -> Path:
    dest_dir = spool_dir / target
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if dest.exists():
        dest = dest_dir / f"{path.stem}.{int(datetime.now().timestamp())}{path.suffix}"
    shutil.move(str(path), str(dest))
    return dest


def run_status(paths: WorkerPaths) -> str:
    result = subprocess.run(
        [sys.executable, str(paths.show_status_script)],
        cwd=str(paths.show_status_script.parent),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RequestError("Status se nepodařilo načíst. Technický detail je v logu.")
    return result.stdout.rstrip() or "Status je prázdný."


def send_status_reply(request_path: Path, paths: WorkerPaths, *, verbose: bool = True) -> None:
    """Run show_status.py, send the reply, and finalize the claimed request."""

    try:
        reply = run_status(paths)
        reply_to_request(reply, request_path, paths)
    except RequestError as exc:
        reply = f"Příkaz se nepodařilo zpracovat: {exc}"
        log(f"status request error for {request_path}: {exc}", paths, verbose=verbose)
        try:
            reply_to_request(reply, request_path, paths)
        finally:
            move_request(request_path, paths.spool_dir, "failed")
        return
    except Exception as exc:  # noqa: BLE001 - asynchronous worker logs full diagnostics.
        reply = "Status se nepodařilo zpracovat kvůli technické chybě. Detail je v logu."
        log(f"status technical error for {request_path}: {exc}\n{traceback.format_exc()}", paths, verbose=verbose)
        try:
            reply_to_request(reply, request_path, paths)
        finally:
            move_request(request_path, paths.spool_dir, "failed")
        return

    move_request(request_path, paths.spool_dir, "done")
    log(f"completed async status {request_path.name}", paths, verbose=verbose)


def start_async_status_reply(request_path: Path, paths: WorkerPaths, *, verbose: bool = True) -> None:
    """Start a detached helper process so the polling checker does not wait for status."""

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--send-status-reply",
        str(request_path),
        "--spool-dir",
        str(paths.spool_dir),
        "--requests-path",
        str(paths.requests_path),
        "--reply-script",
        str(paths.reply_script),
        "--show-status-script",
        str(paths.show_status_script),
        "--log-path",
        str(paths.log_path),
        "--planner-script",
        str(paths.planner_script),
        "--planner-log-path",
        str(paths.planner_log_path),
        "--forecast-path",
        str(paths.forecast_path),
    ]
    if not verbose:
        cmd.append("--quiet")
    subprocess.Popen(cmd, cwd=str(PLANNER_DIR), close_fds=True)
    log(f"async status reply started for {request_path.name}", paths, verbose=verbose)


def _helper_command(paths: WorkerPaths) -> list[str]:
    return [
        "--spool-dir",
        str(paths.spool_dir),
        "--requests-path",
        str(paths.requests_path),
        "--reply-script",
        str(paths.reply_script),
        "--show-status-script",
        str(paths.show_status_script),
        "--log-path",
        str(paths.log_path),
        "--planner-script",
        str(paths.planner_script),
        "--planner-log-path",
        str(paths.planner_log_path),
        "--forecast-path",
        str(paths.forecast_path),
    ]


def start_async_request_planning(
    request_path: Path,
    paths: WorkerPaths,
    *,
    not_before: datetime,
    verbose: bool = True,
) -> None:
    """Start a detached helper that replans, sends the final reply, and finalizes the request."""

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--finish-request-planning",
        str(request_path),
        "--replan-not-before",
        not_before.isoformat(),
        *_helper_command(paths),
    ]
    if not verbose:
        cmd.append("--quiet")
    subprocess.Popen(cmd, cwd=str(PLANNER_DIR), close_fds=True)
    log(f"async request planning started for {request_path.name}", paths, verbose=verbose)


def parse_optional_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt


CZECH_WEEKDAYS = (
    "pondělí",
    "úterý",
    "středa",
    "čtvrtek",
    "pátek",
    "sobota",
    "neděle",
)


def format_user_datetime(value: Any) -> str:
    """Format a timestamp for WhatsApp without seconds or timezone suffix."""

    dt = value if isinstance(value, datetime) else parse_optional_dt(value)
    if dt is None:
        return "neuvedeno"
    return f"{CZECH_WEEKDAYS[dt.weekday()]} {dt.day}. {dt.month}. {dt.year} {dt:%H:%M}"


def format_user_interval(start_value: Any, end_value: Any) -> str:
    start = start_value if isinstance(start_value, datetime) else parse_optional_dt(start_value)
    end = end_value if isinstance(end_value, datetime) else parse_optional_dt(end_value)
    if start is None or end is None:
        return f"{format_user_datetime(start_value)}–{format_user_datetime(end_value)}"
    if start.date() == end.date():
        return f"{format_user_datetime(start)}–{end:%H:%M}"
    return f"{format_user_datetime(start)} až {format_user_datetime(end)}"


def find_current_forecast_slot(forecast: Any, now: datetime) -> dict[str, Any] | None:
    if not isinstance(forecast, dict):
        return None
    slots = forecast.get("slots", [])
    if not isinstance(slots, list):
        return None
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        start = parse_optional_dt(slot.get("slot_start"))
        if start is None:
            continue
        start = start.astimezone(now.tzinfo) if now.tzinfo else start
        end = start + timedelta(minutes=15)
        if start <= now < end:
            return slot
    return None


def request_is_running(item: dict[str, Any], *, now: datetime, forecast: Any) -> bool:
    rtype = item.get("type")
    if rtype == "additional_load":
        start = parse_optional_dt(item.get("start"))
        end = parse_optional_dt(item.get("end"))
        if start is not None and end is not None:
            local_now = now.astimezone(start.tzinfo) if start.tzinfo else now
            return start <= local_now < end

    current_slot = find_current_forecast_slot(forecast, now)
    if current_slot is None:
        return False
    if rtype == "ev_charge":
        return float(current_slot.get("ev_load_kwh", 0.0) or 0.0) > 1e-6
    if rtype == "boiler_full":
        return float(current_slot.get("boiler_power_kw", 0.0) or 0.0) > 1e-6
    return False


def forecast_request(forecast: Any, request_id: str) -> dict[str, Any] | None:
    if not isinstance(forecast, dict):
        return None
    active = forecast.get("active_requests", [])
    if not isinstance(active, list):
        return None
    for item in active:
        if isinstance(item, dict) and item.get("id") == request_id:
            return item
    return None


def describe_request(item: dict[str, Any], forecast: Any = None) -> str:
    rtype = item.get("type") or "unknown"
    if rtype == "ev_charge":
        text = f"auto {item.get('required_ac_kwh', 'n/a')} kWh do {format_user_datetime(item.get('deadline'))}"
        request_id = str(item.get("id") or item.get("request_id") or "")
        planned = forecast_request(forecast, request_id)
        recommendation = planned.get("recommendation", {}) if isinstance(planned, dict) else {}
        if isinstance(recommendation, dict):
            start = recommendation.get("recommended_start")
            end = recommendation.get("expected_end")
            if start and end:
                text += f". Nabíjení naplánovat na {format_user_interval(start, end)}"
            elif recommendation.get("reason"):
                text += f". Zatím bez doporučeného okna: {recommendation['reason']}"
        return text
    if rtype == "boiler_full":
        return f"bojler do {format_user_datetime(item.get('deadline'))}"
    if rtype == "additional_load":
        return (
            f"zátěž {item.get('power_kw', 'n/a')} kW od {format_user_datetime(item.get('start'))} "
            f"do {format_user_datetime(item.get('end'))}"
        )
    return str(rtype)


def build_requests_reply(paths: WorkerPaths) -> str:
    active = request_store.active_requests(paths.requests_path, now=datetime.now().astimezone())
    if not active:
        return "Aktuálně nejsou žádné aktivní požadavky."
    now = datetime.now().astimezone()
    forecast = read_json(paths.forecast_path, {})
    lines = ["Aktivní požadavky:"]
    for idx, item in enumerate(active, start=1):
        running = request_is_running(item, now=now, forecast=forecast)
        suffix = " (právě probíhá)" if running else ""
        lines.append(f"{idx}. {describe_request(item, forecast)}{suffix}")
    lines.append("")
    lines.append("Zrušení: cancel <číslo>, např. cancel 1")
    return "\n".join(lines)


def forecast_is_fresh_and_correlated(
    forecast: Any,
    *,
    not_before: datetime,
    request_id: str | None,
) -> bool:
    if not isinstance(forecast, dict):
        return False
    generated_at = parse_optional_dt(forecast.get("generated_at"))
    if generated_at is None:
        return False
    reference = not_before
    if reference.tzinfo is not None and generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=reference.tzinfo)
    elif reference.tzinfo is None and generated_at.tzinfo is not None:
        reference = reference.replace(tzinfo=generated_at.tzinfo)
    if generated_at < reference:
        return False
    if request_id is not None and forecast_request(forecast, request_id) is None:
        return False
    return True


def run_planner_replan(
    paths: WorkerPaths,
    *,
    request_id: str | None,
    not_before: datetime,
    verbose: bool = True,
    max_attempts: int = PLANNER_REPLAN_MAX_ATTEMPTS,
    retry_seconds: float = PLANNER_REPLAN_RETRY_SECONDS,
) -> dict[str, Any] | None:
    """Run planner until a fresh, request-correlated forecast is available."""

    if not paths.planner_script.exists():
        log(f"planner replan skipped, script missing: {paths.planner_script}", paths, verbose=verbose)
        return None
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            paths.planner_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(paths.planner_log_path, "a", encoding="utf-8") as log_file:
                result = subprocess.run(
                    [sys.executable, str(paths.planner_script), "--dry-run", "--verbose"],
                    cwd=str(paths.planner_script.parent),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
        except OSError as exc:
            log(f"planner replan attempt {attempt}/{attempts} failed: {exc}", paths, verbose=verbose)
        else:
            if result.returncode == 0:
                forecast = read_json(paths.forecast_path, {})
                if forecast_is_fresh_and_correlated(
                    forecast,
                    not_before=not_before,
                    request_id=request_id,
                ):
                    log(f"planner replan confirmed after attempt {attempt}/{attempts}", paths, verbose=verbose)
                    return forecast
                log(
                    f"planner replan attempt {attempt}/{attempts} produced stale or uncorrelated forecast",
                    paths,
                    verbose=verbose,
                )
            else:
                log(
                    f"planner replan attempt {attempt}/{attempts} exited with status {result.returncode}",
                    paths,
                    verbose=verbose,
                )
        if attempt < attempts and retry_seconds > 0:
            time.sleep(retry_seconds)
    return None


def trigger_planner_replan(paths: WorkerPaths, *, verbose: bool = True) -> bool:
    """Start a best-effort detached replan when no correlated reply is required."""

    if not paths.planner_script.exists():
        log(f"planner trigger skipped, script missing: {paths.planner_script}", paths, verbose=verbose)
        return False
    try:
        paths.planner_log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(paths.planner_log_path, "a", encoding="utf-8")
        try:
            subprocess.Popen(
                [sys.executable, str(paths.planner_script), "--dry-run", "--verbose"],
                cwd=str(paths.planner_script.parent),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
        finally:
            log_file.close()
    except OSError as exc:
        log(f"planner trigger failed: {exc}", paths, verbose=verbose)
        return False
    log("planner trigger started asynchronously", paths, verbose=verbose)
    return True


def request_planning_reply(store_request: dict[str, Any], forecast: dict[str, Any]) -> str:
    """Build the final confirmation from a fresh correlated planner forecast."""

    request_id = str(store_request.get("id") or store_request.get("request_id") or "")
    planned = forecast_request(forecast, request_id)
    if planned is None:
        return "Požadavek je uložený, ale v novém plánu se ho nepodařilo potvrdit."

    solver = forecast.get("solver", {})
    solver_status = solver.get("status") if isinstance(solver, dict) else None
    if solver_status != "optimal":
        return f"Požadavek je uložený, ale nový plán není optimální (stav {solver_status or 'neznámý'})."

    rtype = store_request.get("type")
    if rtype == "ev_charge":
        rec = planned.get("recommendation", {})
        if not isinstance(rec, dict):
            return "Požadavek na nabíjení je uložený, ale planner nevrátil doporučené okno."
        original = float(store_request.get("requested_ac_kwh_original", store_request.get("required_ac_kwh", 0.0)) or 0.0)
        effective = float(store_request.get("required_ac_kwh", 0.0) or 0.0)
        limit_note = (
            f" Původně požadovaných {original:g} kWh je kvůli fyzickému limitu jedné relace omezeno na {effective:g} kWh."
            if original > effective else ""
        )
        start = rec.get("recommended_start")
        end = rec.get("expected_end")
        latest = rec.get("latest_safe_start")
        if rec.get("feasible") and start and end:
            return (
                f"Nabití auta o přibližně {effective:g} kWh do "
                f"{format_user_datetime(store_request.get('deadline'))} je zahrnuto v novém plánu. "
                f"Nabíjení nastavte na {format_user_interval(start, end)}. "
                f"Nejpozdější bezpečný start je {format_user_datetime(latest)}.{limit_note}"
            )
        reason = rec.get("reason") or "Planner nenašel proveditelné nabíjecí okno."
        return (
            f"Požadavek na nabití auta do {format_user_datetime(store_request.get('deadline'))} je uložený, "
            f"ale nelze ho nyní plně potvrdit: {reason}"
        )

    if rtype == "boiler_full":
        slacks = forecast.get("optimizer_slacks", {})
        try:
            unserved = float(slacks.get("boiler_hard_unserved_kwh", 0.0) or 0.0)
        except (AttributeError, TypeError, ValueError):
            unserved = 0.0
        if unserved <= 1e-6:
            return (
                f"Požadavek nahřát bojler do {format_user_datetime(store_request.get('deadline'))} "
                "je zahrnutý v novém plánu."
            )
        return (
            f"Požadavek nahřát bojler do {format_user_datetime(store_request.get('deadline'))} je v novém plánu, "
            f"ale podle aktuálních podmínek zbývá nepokryto přibližně {unserved:.1f} kWh."
        )

    if rtype == "additional_load":
        start = parse_optional_dt(store_request.get("start"))
        end = parse_optional_dt(store_request.get("end"))
        slots = forecast.get("slots", [])
        first_slot = parse_optional_dt(slots[0].get("slot_start")) if isinstance(slots, list) and slots else None
        last_slot = parse_optional_dt(slots[-1].get("slot_start")) if isinstance(slots, list) and slots else None
        in_horizon = bool(
            start is not None
            and end is not None
            and first_slot is not None
            and last_slot is not None
            and end > first_slot
            and start < last_slot + timedelta(minutes=15)
        )
        interval = format_user_interval(store_request.get("start"), store_request.get("end"))
        if in_horizon:
            return (
                f"Dodatečná zátěž {store_request.get('power_kw'):g} kW v čase {interval} "
                "je zahrnutá v novém plánu."
            )
        return (
            f"Dodatečná zátěž {store_request.get('power_kw'):g} kW v čase {interval} je uložená. "
            "Interval je zatím mimo aktuální plánovací horizont; planner ji zahrne, jakmile do něj vstoupí."
        )

    return "Požadavek je uložený a nový plán byl úspěšně přepočítán."


def initialize_ev_notification_after_reply(
    store_request: dict[str, Any],
    forecast: dict[str, Any],
    paths: WorkerPaths,
    *,
    notified_at: datetime | None = None,
) -> request_store.EvScheduleNotificationResult | None:
    """Persist the first feasible EV window after its final reply was delivered."""

    if store_request.get("type") != "ev_charge":
        return None
    solver = forecast.get("solver", {})
    if not isinstance(solver, dict) or solver.get("status") != "optimal":
        return None
    request_id = str(store_request.get("id") or store_request.get("request_id") or "")
    planned = forecast_request(forecast, request_id)
    recommendation = planned.get("recommendation", {}) if isinstance(planned, dict) else {}
    if not isinstance(recommendation, dict) or recommendation.get("feasible") is not True:
        return None
    start = recommendation.get("recommended_start")
    end = recommendation.get("expected_end")
    if not isinstance(start, str) or not start or not isinstance(end, str) or not end:
        return None
    stamp = (notified_at or datetime.now().astimezone()).isoformat(timespec="seconds")
    return request_store.initialize_ev_schedule_notification(
        paths.requests_path,
        request_id,
        start=start,
        end=end,
        notified_at=stamp,
    )


def finish_request_planning(
    request_path: Path,
    paths: WorkerPaths,
    *,
    not_before: datetime,
    verbose: bool = True,
) -> None:
    """Complete deferred request planning and preserve spool correlation until the final reply."""

    try:
        payload = load_request(request_path)
        request_id, _message_id, _sender_id, commands, created_at = validate_envelope(payload)
        store_request, _replace_same_type, ok_text = build_store_request(commands[0], request_id, created_at)
        forecast = run_planner_replan(
            paths,
            request_id=request_id,
            not_before=not_before,
            verbose=verbose,
        )
        if forecast is None:
            reply = (
                f"{ok_text} Mimořádný přepočet se nyní nepodařilo potvrdit ani po opakování; "
                "požadavek zůstává aktivní pro další běh planneru."
            )
        else:
            reply = request_planning_reply(store_request, forecast)
        reply_to_request(reply, request_path, paths)
        if forecast is not None:
            baseline_result = initialize_ev_notification_after_reply(store_request, forecast, paths)
            if baseline_result is not None:
                log(
                    f"EV notification baseline {baseline_result.reason} for request {request_id}",
                    paths,
                    verbose=verbose,
                )
    except RequestError as exc:
        reply = f"Finální potvrzení požadavku se nepodařilo vytvořit: {exc}"
        log(f"deferred request error for {request_path}: {exc}", paths, verbose=verbose)
        try:
            reply_to_request(reply, request_path, paths)
        finally:
            move_request(request_path, paths.spool_dir, "failed")
        return
    except Exception as exc:  # noqa: BLE001 - detached helper logs full diagnostics.
        reply = "Finální potvrzení požadavku selhalo kvůli technické chybě. Detail je v logu."
        log(f"deferred request technical error for {request_path}: {exc}\n{traceback.format_exc()}", paths, verbose=verbose)
        try:
            reply_to_request(reply, request_path, paths)
        finally:
            move_request(request_path, paths.spool_dir, "failed")
        return

    move_request(request_path, paths.spool_dir, "done")
    log(f"completed deferred request planning {request_path.name}", paths, verbose=verbose)


def build_store_request(command: str, request_id: str, created_at: str) -> tuple[dict[str, Any], bool, str]:
    match = CHARGE_CAR_RE.match(command)
    if match:
        energy = parse_float(match.group("energy"), "energy_kwh")
        effective_energy = min(energy, MAX_EV_SESSION_KWH)
        deadline = parse_abs_iso(match.group("deadline"), "deadline")
        return (
            {
                "id": request_id,
                "type": "ev_charge",
                "created_at": created_at,
                "available_from": created_at,
                "deadline": deadline,
                "requested_ac_kwh_original": energy,
                "required_ac_kwh": effective_energy,
                "energy_limited_to_vehicle_max": energy > MAX_EV_SESSION_KWH,
                "status": "active",
                "source": "whatsapp",
                "request_id": request_id,
            },
            True,
            (
                f"Požadavek na nabití auta do {format_user_datetime(deadline)} je uložený. "
                + (
                    f"Pro jednu fyzickou relaci plánuji nejvýše {effective_energy:g} kWh "
                    f"místo požadovaných {energy:g} kWh. "
                    if energy > effective_energy else ""
                )
                + "Přepočítávám plán."
            ),
        )

    match = HEAT_BOILER_RE.match(command)
    if match:
        deadline = parse_abs_iso(match.group("deadline"), "deadline")
        return (
            {
                "id": request_id,
                "type": "boiler_full",
                "created_at": created_at,
                "deadline": deadline,
                "status": "active",
                "source": "whatsapp",
                "request_id": request_id,
            },
            True,
            f"Požadavek nahřát bojler do {format_user_datetime(deadline)} je uložený. Přepočítávám plán.",
        )

    match = ADDITIONAL_LOAD_RE.match(command)
    if match:
        power = parse_float(match.group("power"), "power_kw")
        start = parse_abs_iso(match.group("start"), "start")
        end = parse_abs_iso(match.group("end"), "end")
        if datetime.fromisoformat(end) <= datetime.fromisoformat(start):
            raise RequestError("Konec dodatečné zátěže musí být později než začátek.")
        return (
            {
                "id": request_id,
                "type": "additional_load",
                "created_at": created_at,
                "power_kw": power,
                "phase": None,
                "start": start,
                "end": end,
                "description": "whatsapp announced load",
                "status": "active",
                "source": "whatsapp",
                "request_id": request_id,
            },
            False,
            f"Dodatečná zátěž {power:g} kW v čase {format_user_interval(start, end)} je uložená. Přepočítávám plán.",
        )

    raise RequestError(
        "Nerozumím internímu příkazu. Podporuji: status, "
        "charge car;<kWh>kWh;<deadline>, heat boiler;<deadline>, "
        "additional load;<kW>kW;<start>;<end>, requests, cancel <číslo>."
    )


def reply_to_request(text: str, request_path: Path, paths: WorkerPaths) -> None:
    result = subprocess.run(
        [str(paths.reply_script), str(request_path)],
        input=text + "\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"reply_whatsapp.sh failed: stdout={result.stdout!r} stderr={result.stderr!r}")


def process_claimed(path: Path, paths: WorkerPaths, *, verbose: bool = True) -> ProcessOutcome:
    payload = load_request(path)
    request_id, _message_id, _sender_id, commands, created_at = validate_envelope(payload)
    command = commands[0]
    if command == "status":
        if paths.async_status:
            start_async_status_reply(path, paths, verbose=verbose)
            return ProcessOutcome(True, None, deferred=True)
        return ProcessOutcome(True, run_status(paths))
    if command == "requests":
        return ProcessOutcome(True, build_requests_reply(paths))

    match = CANCEL_RE.match(command)
    if match:
        display_id = int(match.group("display_id"))
        active = request_store.active_requests(paths.requests_path)
        if display_id < 1 or display_id > len(active):
            raise RequestError(f"Požadavek číslo {display_id} neexistuje. Zadej `requests` pro aktuální seznam.")
        now = datetime.now().astimezone()
        forecast = read_json(paths.forecast_path, {})
        running = request_is_running(active[display_id - 1], now=now, forecast=forecast)
        result = request_store.cancel_request_by_display_id(
            paths.requests_path,
            display_id,
            running=running,
        )
        if not result.canceled:
            raise RequestError(f"Požadavek číslo {display_id} se nepodařilo zrušit.")
        trigger_planner_replan(paths, verbose=verbose)
        if result.running:
            return ProcessOutcome(True, "Požadavek právě probíhá, ale i tak byl zrušen.")
        return ProcessOutcome(True, "ok")

    store_request, replace_same_type, ok_text = build_store_request(command, request_id, created_at)
    result = request_store.store_request(
        paths.requests_path,
        store_request,
        replace_existing_same_type=replace_same_type,
    )
    if result.duplicate:
        return ProcessOutcome(True, "Tento požadavek už byl dříve přijat, znovu ho nezapisuji.")
    replan_started_at = datetime.now().astimezone()
    if paths.async_planning:
        reply_to_request(ok_text, path, paths)
        start_async_request_planning(
            path,
            paths,
            not_before=replan_started_at,
            verbose=verbose,
        )
        return ProcessOutcome(True, None, deferred=True)
    forecast = run_planner_replan(
        paths,
        request_id=request_id,
        not_before=replan_started_at,
        verbose=verbose,
    )
    if forecast is None:
        return ProcessOutcome(
            True,
            f"{ok_text} Mimořádný přepočet se nyní nepodařilo potvrdit ani po opakování; "
            "požadavek zůstává aktivní pro další běh planneru.",
        )
    return ProcessOutcome(
        True,
        request_planning_reply(store_request, forecast),
        planning_request=store_request,
        planning_forecast=forecast,
    )


def process_one(paths: WorkerPaths, *, verbose: bool = True) -> bool:
    claimed = claim_one(paths.spool_dir)
    if claimed is None:
        if verbose:
            log("no incoming request", paths, verbose=verbose)
        return False

    log(f"claimed {claimed}", paths, verbose=verbose)
    try:
        outcome = process_claimed(claimed, paths, verbose=verbose)
    except RequestError as exc:
        reply = f"Příkaz se nepodařilo zpracovat: {exc}"
        log(f"request error for {claimed}: {exc}", paths, verbose=verbose)
        try:
            reply_to_request(reply, claimed, paths)
        finally:
            move_request(claimed, paths.spool_dir, "failed")
        return True
    except Exception as exc:  # noqa: BLE001 - log full diagnostics, reply safely.
        reply = "Příkaz se nepodařilo zpracovat kvůli technické chybě. Detail je v logu."
        log(f"technical error for {claimed}: {exc}\n{traceback.format_exc()}", paths, verbose=verbose)
        try:
            reply_to_request(reply, claimed, paths)
        finally:
            move_request(claimed, paths.spool_dir, "failed")
        return True

    if outcome.deferred:
        log(f"deferred finalization for {claimed.name}", paths, verbose=verbose)
        return True

    try:
        reply_to_request(outcome.reply or "ok", claimed, paths)
    except Exception as exc:  # noqa: BLE001
        log(f"reply failed for {claimed}: {exc}\n{traceback.format_exc()}", paths, verbose=verbose)
        move_request(claimed, paths.spool_dir, "failed")
        return True

    if outcome.planning_request is not None and outcome.planning_forecast is not None:
        baseline_result = initialize_ev_notification_after_reply(
            outcome.planning_request,
            outcome.planning_forecast,
            paths,
        )
        if baseline_result is not None:
            log(
                f"EV notification baseline {baseline_result.reason} for request {baseline_result.request_id}",
                paths,
                verbose=verbose,
            )

    move_request(claimed, paths.spool_dir, "done")
    log(f"completed {claimed.name} success={outcome.success}", paths, verbose=verbose)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Process validated WhatsApp spool requests for planner_v11")
    parser.add_argument("--spool-dir", type=Path, default=DEFAULT_SPOOL_DIR)
    parser.add_argument("--requests-path", type=Path, default=DEFAULT_REQUESTS_PATH)
    parser.add_argument("--reply-script", type=Path, default=DEFAULT_REPLY_SCRIPT)
    parser.add_argument("--show-status-script", type=Path, default=DEFAULT_SHOW_STATUS)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--planner-script", type=Path, default=DEFAULT_PLANNER_SCRIPT)
    parser.add_argument("--planner-log-path", type=Path, default=DEFAULT_PLANNER_LOG_PATH)
    parser.add_argument("--forecast-path", type=Path, default=DEFAULT_FORECAST_PATH)
    parser.add_argument("--send-status-reply", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--finish-request-planning", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--replan-not-before", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--max-requests", type=int, default=20, help="Maximum requests processed per run")
    parser.add_argument("--poll-iterations", type=int, default=5, help="How many polling cycles to run before exiting")
    parser.add_argument("--poll-interval-seconds", type=float, default=10.0, help="Sleep between polling cycles")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    paths = WorkerPaths(
        spool_dir=args.spool_dir,
        requests_path=args.requests_path,
        reply_script=args.reply_script,
        show_status_script=args.show_status_script,
        log_path=args.log_path,
        planner_script=args.planner_script,
        planner_log_path=args.planner_log_path,
        forecast_path=args.forecast_path,
    )

    if args.send_status_reply is not None:
        send_status_reply(args.send_status_reply, paths, verbose=not args.quiet)
        return 0

    if args.finish_request_planning is not None:
        not_before = parse_optional_dt(args.replan_not_before)
        if not_before is None:
            print("CHYBA: --finish-request-planning vyžaduje platný --replan-not-before", file=sys.stderr)
            return 2
        finish_request_planning(
            args.finish_request_planning,
            paths,
            not_before=not_before,
            verbose=not args.quiet,
        )
        return 0

    processed = 0
    iterations = max(1, args.poll_iterations)
    for iteration in range(iterations):
        batch_processed = 0
        for _ in range(max(1, args.max_requests)):
            if not process_one(paths, verbose=not args.quiet):
                break
            processed += 1
            batch_processed += 1
        if iteration < iterations - 1:
            time.sleep(max(0.0, args.poll_interval_seconds))
    if processed > 0 or not args.quiet:
        log(f"run finished processed={processed}", paths, verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())