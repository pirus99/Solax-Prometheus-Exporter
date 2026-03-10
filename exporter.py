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

import logging
import os
import time

import requests
from dotenv import load_dotenv
from prometheus_client import Gauge, start_http_server

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
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
    },
    "solax_daily_energy_kwh": {
        "index": 13,
        "scale": 0.1,
        "unit": "kWh",
        "description": "Daily Energy Generated",
    },
}

INVERTER_FIELDS: dict = {
    "solax_inverter_status": {
        "index": 10,
        "scale": 1.0,
        "unit": "",
        "description": (
            "Inverter Status Code "
            "(0=Waiting, 1=Checking, 2=Normal, 3=Fault, 4=Permanent Fault)"
        ),
    },
    "solax_inverter_temperature_celsius": {
        "index": 38,
        "scale": 1.0,
        "unit": "°C",
        "description": "Inverter Temperature",
    },
}

# Smart-meter fields — only collected when ENABLE_SMART_METER=true.
# These indices are based on Solax X1 Air Mini with an attached smart meter
# and may differ on other models.  Adjust "index" and/or "scale" as needed
# once you verify the readings against your own inverter data.
SMART_METER_FIELDS: dict = {
    # Feed-in power: positive value = exporting to grid,
    #                negative value = importing from grid.
    "solax_feedin_power_watts": {
        "index": 40,
        "scale": 1.0,
        "unit": "W",
        "description": (
            "Grid Feed-in Power "
            "(positive = export to grid, negative = import from grid)"
        ),
        "signed": True,
    },
    # Instantaneous house consumption power.
    "solax_load_power_watts": {
        "index": 47,
        "scale": 1.0,
        "unit": "W",
        "description": "House Load / Consumption Power",
    },
    # Cumulative energy totals from smart meter.
    "solax_total_feed_energy_kwh": {
        "index": 49,
        "scale": 0.1,
        "unit": "kWh",
        "description": "Total Energy Exported to Grid (lifetime)",
    },
    "solax_total_import_energy_kwh": {
        "index": 51,
        "scale": 0.1,
        "unit": "kWh",
        "description": "Total Energy Imported from Grid (lifetime)",
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
# Helpers
# ---------------------------------------------------------------------------

def to_signed16(value: int) -> int:
    """Convert an unsigned 16-bit integer to a signed 16-bit integer."""
    return value - 65536 if value > 32767 else value


def load_inverter_config() -> list:
    """
    Build the list of inverter configurations from environment variables.

    Expected variables (N is 1-based, up to INVERTER_COUNT):
        INVERTER_N_NAME     – Prometheus label value (default: inverter_N)
        INVERTER_N_ENDPOINT – Full HTTP URL of the inverter (required)
        INVERTER_N_SERIAL   – Serial number used as API password (required)
    """
    inverters = []
    for i in range(1, INVERTER_COUNT + 1):
        name = os.getenv(f"INVERTER_{i}_NAME", f"inverter_{i}")
        endpoint = os.getenv(f"INVERTER_{i}_ENDPOINT", "").strip()
        serial = os.getenv(f"INVERTER_{i}_SERIAL", "").strip()

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

        inverters.append({"name": name, "endpoint": endpoint, "serial": serial})
        logger.info(
            "Configured inverter %d: name=%s, endpoint=%s", i, name, endpoint
        )

    return inverters


def create_gauges(active_fields: dict) -> dict:
    """Create one Prometheus Gauge per active field, labelled by inverter name."""
    gauges = {}
    for metric_name, field in active_fields.items():
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
        logger.error("Request to %s failed: %s", endpoint, exc)
    except ValueError as exc:
        logger.error("JSON decode error from %s: %s", endpoint, exc)
    return None


def update_metrics(
    gauges: dict, inverters: list, active_fields: dict
) -> None:
    """Poll every inverter and push the latest values into the Gauges."""
    for inverter in inverters:
        name = inverter["name"]
        data = fetch_inverter_data(inverter["endpoint"], inverter["serial"])

        if data is None:
            logger.warning("No data received from inverter '%s'", name)
            continue

        raw_data: list = data.get("Data", [])
        if not raw_data:
            logger.warning("Empty Data array from inverter '%s'", name)
            continue

        logger.debug(
            "Received %d data points from inverter '%s'", len(raw_data), name
        )

        for metric_name, field in active_fields.items():
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Starting Solax Prometheus Exporter")
    logger.info("  Port           : %d", EXPORTER_PORT)
    logger.info("  Scrape interval: %d s", SCRAPE_INTERVAL)
    logger.info("  Smart meter    : %s", ENABLE_SMART_METER)

    inverters = load_inverter_config()
    if not inverters:
        logger.error(
            "No valid inverters configured. "
            "Set INVERTER_1_ENDPOINT and INVERTER_1_SERIAL in your .env file."
        )
        return

    logger.info("Monitoring %d inverter(s)", len(inverters))

    active_fields = dict(STANDARD_FIELDS)
    if ENABLE_SMART_METER:
        active_fields.update(SMART_METER_FIELDS)

    gauges = create_gauges(active_fields)
    start_http_server(EXPORTER_PORT)
    logger.info(
        "Metrics available at http://0.0.0.0:%d/metrics", EXPORTER_PORT
    )

    while True:
        update_metrics(gauges, inverters, active_fields)
        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()
