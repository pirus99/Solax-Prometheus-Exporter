#!/usr/bin/env python3
"""
Solax Prometheus Exporter
=========================
Exports real-time data from Solax inverters to Prometheus.

Supports 1–200 Solax inverter instances configurable via a .env file.

API request format used by each inverter poll:
    POST <INVERTER_N_ENDPOINT>
    Content-Type: application/x-www-form-urlencoded
    Body: optType=ReadRealTimeData&pwd=<INVERTER_N_SERIAL>

Example response:
    {
      "sn": "SERIALNO",
      "ver": "3.012.01",
      "type": 4,
      "Data": [2393, 14, 354, 648, 0, 57, 0, 372, 0, 5002, 2, 11159, 0, 15, ...],
      "Information": [0.600, 4, "XM3A06IA600428", 8, 2.27, 0.00, 1.43, 0.00, 0.00, 1]
    }
"""

from __future__ import annotations

import datetime
import logging
import os
import sys
import time

import requests
from dotenv import load_dotenv
from prometheus_client import Gauge, start_http_server

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
load_dotenv()

# DEBUG_LOG must be read before basicConfig so the level is set correctly.
DEBUG_LOG: bool = os.getenv("DEBUG_LOG", "false").lower() == "true"

logging.basicConfig(
    level=logging.DEBUG if DEBUG_LOG else logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global configuration (from .env / environment)
# ---------------------------------------------------------------------------
EXPORTER_PORT = int(os.getenv("EXPORTER_PORT", "9101"))
SCRAPE_INTERVAL = int(os.getenv("SCRAPE_INTERVAL", "30"))
# Clamp to the supported 1–200 range to prevent accidental over-polling.
INVERTER_COUNT = max(1, min(200, int(os.getenv("INVERTER_COUNT", "1"))))
ENABLE_SMART_METER = os.getenv("ENABLE_SMART_METER", "false").lower() == "true"
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))
# Number of consecutive unreachable polls before real-time readings are zeroed.
OFFLINE_FAILURE_THRESHOLD = int(os.getenv("OFFLINE_FAILURE_THRESHOLD", "5"))

# ---------------------------------------------------------------------------
# DATA FIELD MAPPINGS
#
# Verified against Solax X1 Air Mini / X1 Boost Mini (local API, type 4).
# Other models (X3, Hybrid) may have different index assignments — simply
# change the "index" value in the relevant entry.
#
# Each field entry contains:
#   index       – position in the Data[] array from the inverter response
#   scale       – multiply the raw integer by this to obtain the real value
#                   Voltage   : raw / 10  → Volts
#                   Current   : raw / 10  → Amps
#                   Power     : raw * 1   → Watts
#                   Frequency : raw / 100 → Hz
#                   Energy    : raw / 10  → kWh
#                   Temp      : raw * 1   → °C
#   unit        – physical unit string (for documentation only)
#   description – Prometheus metric help text
#   signed      – (optional) True when the value can be negative.
#                 Negative numbers are stored as unsigned 16-bit two's
#                 complement (i.e. value > 32767 → value − 65536).
#   persist_offline – (optional) True for accumulated totals (kWh) that must
#                 NOT be zeroed when the inverter has been unreachable for
#                 OFFLINE_FAILURE_THRESHOLD consecutive polls.  All other
#                 fields are zeroed to indicate "no live data".
# ---------------------------------------------------------------------------

GRID_FIELDS: dict = {
    "solax_grid_voltage_volts": {
        "index": 0,
        "scale": 0.1,
        "unit": "V",
        "description": "Grid / Mains Voltage",
    },
    "solax_grid_current_amperes": {
        "index": 1,
        "scale": 0.1,
        "unit": "A",
        "description": "Grid / Mains Current",
    },
    "solax_grid_power_watts": {
        "index": 2,
        "scale": 1.0,
        "unit": "W",
        "description": "Inverter AC Output Power",
    },
    "solax_grid_frequency_hertz": {
        "index": 9,
        "scale": 0.01,
        "unit": "Hz",
        "description": "Grid Frequency",
    },
}

PV_FIELDS: dict = {
    "solax_pv1_voltage_volts": {
        "index": 3,
        "scale": 0.1,
        "unit": "V",
        "description": "PV String 1 Voltage",
    },
    "solax_pv2_voltage_volts": {
        "index": 4,
        "scale": 0.1,
        "unit": "V",
        "description": "PV String 2 Voltage",
    },
    "solax_pv1_current_amperes": {
        "index": 5,
        "scale": 0.1,
        "unit": "A",
        "description": "PV String 1 Current",
    },
    "solax_pv2_current_amperes": {
        "index": 6,
        "scale": 0.1,
        "unit": "A",
        "description": "PV String 2 Current",
    },
    "solax_pv1_power_watts": {
        "index": 7,
        "scale": 1.0,
        "unit": "W",
        "description": "PV String 1 Power",
    },
    "solax_pv2_power_watts": {
        "index": 8,
        "scale": 1.0,
        "unit": "W",
        "description": "PV String 2 Power",
    },
}

ENERGY_FIELDS: dict = {
    "solax_total_energy_kwh": {
        "index": 11,
        "scale": 0.1,
        "unit": "kWh",
        "description": "Total Lifetime Energy Generated",
        "persist_offline": True,
    },
    "solax_daily_energy_kwh": {
        "index": 13,
        "scale": 0.1,
        "unit": "kWh",
        "description": "Daily Energy Generated",
        "persist_offline": True,
    },
}

INVERTER_FIELDS: dict = {
    "solax_inverter_status": {
        "index": 10,
        "scale": 1.0,
        "unit": "",
        "description": (
            "Inverter Status Code "
            "(-1=Offline/Unreachable, 0=Waiting, 1=Checking, 2=Normal, "
            "3=Fault, 4=Permanent Fault)"
        ),
    },
    "solax_inverter_temperature_celsius": {
        "index": 39,
        "scale": 1.0,
        "unit": "°C",
        "description": "Inverter Temperature",
    },
}

# Smart-meter fields — only collected when ENABLE_SMART_METER=true.
# These indices are based on Solax X1 Air Mini with an attached smart meter
# and may differ on other models.  Adjust "index" and/or "scale" as needed
# once you verify the readings against your own inverter data.
# House Load / Consumption Power = field 49 - field 50 = positive = feed-in, negative = load
SMART_METER_FIELDS: dict = {
    # Feed-in power: positive value = exporting to grid,
    #                negative value = importing from grid.
    "solax_feedin_power_watts_1": {
        "index": 48,
        "scale": 1.0,
        "unit": "W",
        "persist_offline": True,
        "description": "Smart-meter feed-in power (channel 1)",
    },
    "solax_feedin_power_watts_2": {
        "index": 49,
        "scale": 1.0,
        "unit": "W",
        "persist_offline": True,
        "description": "Smart-meter feed-in power (channel 2)",
    },
    "solax_feedin_power_watts": {
        "scale": 1.0,
        "unit": "W",
        "description": (
            "Grid Feed-in Power "
            "(positive = export to grid, negative = import from grid)"
        ),
        "signed": True,
    },
    # Cumulative energy totals from smart meter.
    "solax_total_feed_energy_kwh": {
        "index": 50,
        "scale": 0.01,
        "unit": "kWh",
        "description": "Total Energy Exported to Grid (lifetime)",
        "persist_offline": True,
    },
    "solax_total_import_energy_kwh": {
        "index": 41,
        "scale": 0.01,
        "unit": "kWh",
        "description": "Total Energy Imported from Grid (lifetime)",
        "persist_offline": True,
    },
}

# Merge all standard (non-smart-meter) fields into one dict for convenience.
STANDARD_FIELDS: dict = {
    **GRID_FIELDS,
    **PV_FIELDS,
    **ENERGY_FIELDS,
    **INVERTER_FIELDS,
}


# ---------------------------------------------------------------------------
# Status window (non-debug mode)
# ---------------------------------------------------------------------------

# Tracks how many lines the last status block occupied so the next render can
# overwrite them in-place on an interactive terminal.
_STATUS_LINE_COUNT: int = 0


def _is_tty() -> bool:
    """Return True when stdout is an interactive terminal (not a pipe/file)."""
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def print_status_window(inverters: list) -> None:
    """Render an in-place status summary for every inverter.

    On an interactive TTY the block is redrawn over itself using ANSI cursor
    control so the terminal acts like a live status display.  In non-TTY
    environments (Docker, systemd, pipe) the block is simply written once per
    poll cycle — each block is clean and self-contained, which is far less
    noisy than a new warning line for every single failure.
    """
    global _STATUS_LINE_COUNT

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # local time for display

    # Build the lines that form the status block.
    separator = "─" * 68
    lines: list[str] = [
        f"┌{separator}┐",
        f"│  Solax Exporter  ──  {now:<46s}│",
        f"├{'─'*22}┬{'─'*27}┬{'─'*17}┤",
        f"│  {'Inverter':<20s}│  {'Status':<25s}│  {'Last OK':<15s}│",
        f"├{'─'*22}┼{'─'*27}┼{'─'*17}┤",
    ]

    for inv in inverters:
        name: str = inv["name"]
        failures: int = inv.get("consecutive_failures", 0)
        last_ok: datetime.datetime | None = inv.get("last_success_time")
        # Show full date when the last success was on a different calendar day
        # so multi-day outages are immediately obvious.
        if last_ok is None:
            last_ok_str = "never"
        elif last_ok.date() == datetime.date.today():
            last_ok_str = last_ok.strftime("%H:%M:%S")
        else:
            last_ok_str = last_ok.strftime("%m-%d %H:%M")

        if failures == 0:
            state = "Online"
        else:
            plural = "s" if failures != 1 else ""
            state = f"Offline ({failures} failure{plural})"

        lines.append(
            f"│  {name:<20s}│  {state:<25s}│  {last_ok_str:<15s}│"
        )

    lines.append(f"└{'─'*22}┴{'─'*27}┴{'─'*17}┘")

    if _is_tty() and _STATUS_LINE_COUNT > 0:
        # Move the cursor up to the first line of the previous block and erase
        # everything from there to the end of the screen.
        sys.stdout.write(f"\033[{_STATUS_LINE_COUNT}A\033[J")

    output = "\n".join(lines) + "\n"
    sys.stdout.write(output)
    sys.stdout.flush()
    _STATUS_LINE_COUNT = len(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_signed16(value: int) -> int:
    """Convert an unsigned 16-bit integer to a signed 16-bit integer."""
    return value - 65536 if value > 32767 else value


def load_inverter_config() -> list:
    """
    Build the list of inverter configurations from environment variables.

    Expected variables (N is 1-based, up to INVERTER_COUNT):
        INVERTER_N_NAME         – Prometheus label value (default: inverter_N)
        INVERTER_N_ENDPOINT     – Full HTTP URL of the inverter (required)
        INVERTER_N_SERIAL       – Serial number used as API password (required)
        INVERTER_N_SMART_METER  – Enable smart-meter readings for this inverter
                                  (default: value of global ENABLE_SMART_METER)
    """
    inverters = []
    for i in range(1, INVERTER_COUNT + 1):
        name = os.getenv(f"INVERTER_{i}_NAME", f"inverter_{i}")
        endpoint = os.getenv(f"INVERTER_{i}_ENDPOINT", "").strip()
        serial = os.getenv(f"INVERTER_{i}_SERIAL", "").strip()
        smart_meter_default = "true" if ENABLE_SMART_METER else "false"
        smart_meter = (
            os.getenv(f"INVERTER_{i}_SMART_METER", smart_meter_default).lower()
            == "true"
        )

        if not endpoint:
            logger.warning(
                "INVERTER_%d_ENDPOINT is not set — skipping inverter %d", i, i
            )
            continue
        if not serial:
            logger.warning(
                "INVERTER_%d_SERIAL is not set — skipping inverter %d", i, i
            )
            continue

        inverters.append(
            {
                "name": name,
                "endpoint": endpoint,
                "serial": serial,
                "smart_meter": smart_meter,
                # Tracks consecutive unreachable polls for this inverter.
                "consecutive_failures": 0,
                # Timestamp of the last successful data fetch (None = never).
                "last_success_time": None,
            }
        )
        logger.info(
            "Configured inverter %d: name=%s, endpoint=%s, smart_meter=%s",
            i,
            name,
            endpoint,
            smart_meter,
        )

    return inverters


def create_gauges(fields: dict) -> dict:
    """Create one Prometheus Gauge per field entry, labelled by inverter name."""
    gauges = {}
    for metric_name, field in fields.items():
        gauges[metric_name] = Gauge(
            metric_name,
            field["description"],
            ["inverter"],
        )
    return gauges


def fetch_inverter_data(endpoint: str, serial: str) -> dict | None:
    """
    POST optType=ReadRealTimeData&pwd=<serial> to the inverter endpoint.

    Returns the parsed JSON dict on success, or None on any failure.
    In non-debug mode errors are logged at DEBUG level to avoid spamming the
    console — the status window shows the offline state instead.
    """
    try:
        response = requests.post(
            endpoint,
            data=f"optType=ReadRealTimeData&pwd={serial}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as exc:
        if DEBUG_LOG:
            logger.error("Request to %s failed: %s", endpoint, exc)
        else:
            logger.debug("Request to %s failed: %s", endpoint, exc)
    except ValueError as exc:
        if DEBUG_LOG:
            logger.error("JSON decode error from %s: %s", endpoint, exc)
        else:
            logger.debug("JSON decode error from %s: %s", endpoint, exc)
    return None


def _active_fields_for(inverter: dict) -> dict:
    """Return the combined field dict that applies to a single inverter."""
    fields = dict(STANDARD_FIELDS)
    if inverter["smart_meter"]:
        fields.update(SMART_METER_FIELDS)
    return fields


def update_metrics(gauges: dict, inverters: list) -> None:
    """Poll every inverter and push the latest values into the Gauges."""
    for inverter in inverters:
        name = inverter["name"]
        data = fetch_inverter_data(inverter["endpoint"], inverter["serial"])

        if data is None:
            inverter["consecutive_failures"] += 1
            failures = inverter["consecutive_failures"]

            if DEBUG_LOG:
                # Verbose mode: log every failure as before.
                logger.warning(
                    "No data from inverter '%s' (consecutive failures: %d)",
                    name,
                    failures,
                )
            elif failures == 1:
                # Non-debug mode: log the transition to offline exactly once;
                # subsequent failures are silent (shown via status window only).
                logger.warning("Inverter '%s' is offline.", name)

            # Always mark the inverter as offline (-1).
            if "solax_inverter_status" in gauges:
                gauges["solax_inverter_status"].labels(inverter=name).set(-1)

            # After OFFLINE_FAILURE_THRESHOLD consecutive failures, zero every
            # real-time reading so stale values are not reported as live data.
            # kWh accumulators (persist_offline=True) are intentionally kept.
            if failures >= OFFLINE_FAILURE_THRESHOLD:
                active = _active_fields_for(inverter)
                for metric_name, field in active.items():
                    if metric_name == "solax_inverter_status":
                        continue  # already set to -1 above
                    if metric_name not in gauges:
                        continue
                    if not field.get("persist_offline"):
                        gauges[metric_name].labels(inverter=name).set(0)
            continue

        # ── Successful response ────────────────────────────────────────────
        was_offline = inverter["consecutive_failures"] > 0
        inverter["consecutive_failures"] = 0
        inverter["last_success_time"] = datetime.datetime.now()  # local time for display

        if was_offline:
            logger.info("Inverter '%s' came back online.", name)

        raw_data: list = data.get("Data", [])
        if not raw_data:
            logger.warning("Empty Data array from inverter '%s'", name)
            continue

        logger.debug(
            "Received %d data points from inverter '%s'", len(raw_data), name
        )

        for metric_name, field in _active_fields_for(inverter).items():
            # Skip computed/derived metrics that don't have a direct index.
            if "index" not in field:
                continue

            idx: int = field["index"]
            if idx >= len(raw_data):
                logger.debug(
                    "Index %d out of range for inverter '%s' (data length: %d)",
                    idx,
                    name,
                    len(raw_data),
                )
                continue

            raw_value: int = raw_data[idx]
            if field.get("signed"):
                raw_value = to_signed16(raw_value)

            gauges[metric_name].labels(inverter=name).set(
                raw_value * field["scale"]
            )

        # Compute derived smart-meter feed-in power if applicable.
        # Feed-in = channel_1 (field 49) - channel_2 (field 50).
        if inverter.get("smart_meter") and "solax_feedin_power_watts" in gauges:
            idx1 = SMART_METER_FIELDS.get("solax_feedin_power_watts_1", {}).get("index")
            idx2 = SMART_METER_FIELDS.get("solax_feedin_power_watts_2", {}).get("index")
            if idx1 is not None and idx2 is not None and idx1 < len(raw_data) and idx2 < len(raw_data):
                raw1 = to_signed16(raw_data[idx1])
                raw2 = to_signed16(raw_data[idx2])
                diff = raw1 - raw2
                scale = SMART_METER_FIELDS["solax_feedin_power_watts"]["scale"]
                gauges["solax_feedin_power_watts"].labels(inverter=name).set(diff * scale)
            else:
                logger.debug(
                    "Not enough smart meter data to compute feed-in for '%s'",
                    name,
                )

    # In non-debug mode refresh the status window after every full poll cycle.
    if not DEBUG_LOG:
        print_status_window(inverters)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Starting Solax Prometheus Exporter")
    logger.info("  Port              : %d", EXPORTER_PORT)
    logger.info("  Scrape interval   : %d s", SCRAPE_INTERVAL)
    logger.info("  Global smart meter: %s", ENABLE_SMART_METER)
    logger.info("  Offline threshold : %d consecutive failures", OFFLINE_FAILURE_THRESHOLD)
    logger.info("  Debug log         : %s", DEBUG_LOG)

    inverters = load_inverter_config()
    if not inverters:
        logger.error(
            "No valid inverters configured. "
            "Set INVERTER_1_ENDPOINT and INVERTER_1_SERIAL in your .env file."
        )
        return

    logger.info("Monitoring %d inverter(s)", len(inverters))

    # Create gauges for every field that is needed by at least one inverter.
    all_fields = dict(STANDARD_FIELDS)
    if any(inv["smart_meter"] for inv in inverters):
        all_fields.update(SMART_METER_FIELDS)

    gauges = create_gauges(all_fields)
    start_http_server(EXPORTER_PORT)
    logger.info(
        "Metrics available at http://0.0.0.0:%d/metrics", EXPORTER_PORT
    )

    while True:
        update_metrics(gauges, inverters)
        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()
