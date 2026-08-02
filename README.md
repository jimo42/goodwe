# goodwe

Automation for a home photovoltaic system with a GoodWe inverter, battery storage and controlled electrical loads.

The project started as a small set of Linux shell scripts and was later rebuilt into a smarter Python-based planner/executor architecture.

## What I have

- GoodWe inverter monitoring and control.
- Battery scheduling through GoodWe ECO modes.
- 48-hour planning horizon with 15-minute resolution.
- Electricity price input used for import/export and charging decisions.
- Weather forecast input used for PV-aware planning.
- MILP-based planner for battery, boiler and selected flexible loads.
- Dynamic executor that applies the current plan every few minutes.
- Water heater control through a network relay with read-back checks.
- Runtime safety gates, dry-run support, state files and failure counters.
- Basic request handling for ad-hoc EV charging, boiler heating and additional loads.
- Daily/status reporting and operational alerts.
- Hermetic manual test suite for planner, executor, device adapters and models.

## What I'd like to have

- Nothing specific planned at the moment.

## Notes

- Local configuration, credentials, logs, runtime state, generated data and backups are intentionally excluded from Git.
- Production-specific values should live in local ignored configuration files, not in committed source code.