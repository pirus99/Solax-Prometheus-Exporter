# Solax-Prometheus-Exporter

A Python-based Prometheus exporter for **Solax X-Series inverters** that use the local HTTP API (no cloud required).  
It supports **1–200 inverter instances** in a single process, each identified by a configurable Prometheus label.

---

## Features

- Polls every inverter via `POST optType=ReadRealTimeData&pwd=<SERIAL>` (the same request as the Solax local API).
- Exports real-time metrics: grid voltage/current/power/frequency, PV string data, energy totals, inverter status, and temperature.
- Optional **smart-meter metrics** (feed-in power, load power, import/export energy totals) — configurable **globally** or **per inverter**.
- **Offline-resilient**: if an inverter is unreachable the exporter never crashes; `solax_inverter_status` is set to `-1` and, after a configurable number of consecutive failures, all real-time readings are zeroed (accumulated kWh totals are always preserved).
- All **Data\[\] index assignments are in one place** in `exporter.py` — easy to remap if your model differs.
- Configurable entirely via a `.env` file.
- Docker-ready (`Dockerfile` + `docker-compose.yml` included).

---

## Quick start

### 1. Clone and install dependencies

```bash
git clone https://github.com/pirus99/Solax-Prometheus-Exporter.git
cd Solax-Prometheus-Exporter
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your inverter endpoint(s) and serial number(s)
```

### 3. Run

```bash
python exporter.py
```

Metrics are available at `http://localhost:9090/metrics`.

---

## Docker

```bash
cp .env.example .env
# Edit .env …
docker compose up -d
```

---

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `INVERTER_COUNT` | `1` | Number of inverters to monitor (1–200). Each inverter adds one HTTP request per scrape cycle. With a 30 s interval and 200 inverters, each inverter gets ~0.15 s of the poll window — ensure `REQUEST_TIMEOUT` × `INVERTER_COUNT` < `SCRAPE_INTERVAL`. |
| `INVERTER_N_NAME` | `inverter_N` | Prometheus label for inverter N |
| `INVERTER_N_ENDPOINT` | *(required)* | Full HTTP URL, e.g. `http://192.168.1.100` |
| `INVERTER_N_SERIAL` | *(required)* | Inverter serial number (also the API password) |
| `INVERTER_N_SMART_METER` | *(global default)* | Enable smart-meter readings for inverter N only. Overrides `ENABLE_SMART_METER` for that inverter. |
| `EXPORTER_PORT` | `9101` | TCP port Prometheus scrapes (9101 avoids clashing with Prometheus itself on 9090) |
| `SCRAPE_INTERVAL` | `30` | Poll interval in seconds |
| `REQUEST_TIMEOUT` | `10` | Per-request HTTP timeout in seconds |
| `OFFLINE_FAILURE_THRESHOLD` | `5` | Consecutive failed polls before real-time readings are zeroed (kWh totals are always preserved) |
| `ENABLE_SMART_METER` | `false` | Global default for smart-meter metrics (can be overridden per inverter with `INVERTER_N_SMART_METER`) |
| `DEBUG_LOG` | `false` | When `false` (default): a compact **status window** is printed/refreshed each poll cycle instead of per-failure log lines. When `true`: full verbose logging is written (request errors, data-point counts, etc.) and the status window is disabled. Useful for troubleshooting. |

**Multiple inverters** — set `INVERTER_COUNT=2` and add `INVERTER_2_NAME`, `INVERTER_2_ENDPOINT`, `INVERTER_2_SERIAL` (and so on up to 200).

---

## Exported metrics

All metrics carry an `inverter` label with the value of `INVERTER_N_NAME`.

### Standard metrics (always exported)

| Metric | Unit | Data\[\] index | Scale |
|---|---|---|---|
| `solax_grid_voltage_volts` | V | 0 | ÷ 10 |
| `solax_grid_current_amperes` | A | 1 | ÷ 10 |
| `solax_grid_power_watts` | W | 2 | × 1 |
| `solax_grid_frequency_hertz` | Hz | 9 | ÷ 100 |
| `solax_pv1_voltage_volts` | V | 3 | ÷ 10 |
| `solax_pv2_voltage_volts` | V | 4 | ÷ 10 |
| `solax_pv1_current_amperes` | A | 5 | ÷ 10 |
| `solax_pv2_current_amperes` | A | 6 | ÷ 10 |
| `solax_pv1_power_watts` | W | 7 | × 1 |
| `solax_pv2_power_watts` | W | 8 | × 1 |
| `solax_total_energy_kwh` | kWh | 11 | ÷ 10 |
| `solax_daily_energy_kwh` | kWh | 13 | ÷ 10 |
| `solax_inverter_status` | — | 10 | × 1 |
| `solax_inverter_temperature_celsius` | °C | 39 | × 1 |

Status codes: `-1` = Offline/Unreachable, `0` = Waiting, `1` = Checking, `2` = Normal, `3` = Fault, `4` = Permanent Fault.

### Smart-meter metrics (`ENABLE_SMART_METER=true` or `INVERTER_N_SMART_METER=true`)

| Metric | Unit | Data\[\] index | Scale | Notes |
|---|---|---|---|---|
| `solax_feedin_power_watts` | W | 41 | × 1 | Positive = export, negative = import |
| `solax_load_power_watts` | W | 48 | × 1 | House consumption |
| `solax_total_feed_energy_kwh` | kWh | 50 | ÷ 10 | Lifetime export total |
| `solax_total_import_energy_kwh` | kWh | 52 | ÷ 10 | Lifetime import total |

> **Note:** Smart-meter indices are based on the Solax X1 Air Mini (API type 4).  
> If the values look wrong for your model, adjust the `"index"` values in the  
> `SMART_METER_FIELDS` dictionary at the top of `exporter.py`.

---

## Offline / error handling

When an inverter is unreachable (evening shutdown, network issue, etc.) the
exporter **never crashes** — it keeps polling every `SCRAPE_INTERVAL` seconds.

| Consecutive failures | Behaviour |
|---|---|
| 1 | `solax_inverter_status` set to **-1** (Offline/Unreachable) |
| 2 – (`OFFLINE_FAILURE_THRESHOLD` − 1) | Status stays **-1**, all other metrics keep their last live value |
| ≥ `OFFLINE_FAILURE_THRESHOLD` (default 5) | Status stays **-1**, all **real-time** readings set to **0** |
| Recovery (next successful poll) | Failure counter resets; all metrics return to live values |

**What is preserved even at 5+ failures:**  
`solax_total_energy_kwh`, `solax_daily_energy_kwh`, `solax_total_feed_energy_kwh`,
`solax_total_import_energy_kwh` — these are accumulated totals that remain valid
even when the inverter is offline.

**Tune the threshold** with `OFFLINE_FAILURE_THRESHOLD` in your `.env`.

---

## Logging / status window

By default (`DEBUG_LOG=false`) the exporter avoids log spam by showing a
compact **status window** instead of printing a new warning line on every
failed poll.  The window is refreshed once per `SCRAPE_INTERVAL` and looks
like this:

```
┌────────────────────────────────────────────────────────────────────┐
│  Solax Exporter  ──  2026-03-11 18:00:00                           │
├──────────────────────┬───────────────────────────┬─────────────────┤
│  Inverter            │  Status                   │  Last OK        │
├──────────────────────┼───────────────────────────┼─────────────────┤
│  inverter_1          │  Online                   │  18:00:00       │
│  inverter_2          │  Offline (3 failures)     │  17:45:30       │
└──────────────────────┴───────────────────────────┴─────────────────┘
```

- On an **interactive terminal (TTY)** the block is redrawn in-place using
  ANSI cursor control — it behaves like a live dashboard.
- In **non-TTY environments** (Docker, systemd, pipe) the block is simply
  appended once per cycle without ANSI codes.  This is still far less noisy
  than a repeated failure line every 30 s.

State-change events (inverter went offline, came back online) are always
logged as WARNING / INFO regardless of this setting.

To enable **full verbose logging** and disable the status window, set
`DEBUG_LOG=true` in your `.env`.

---

## Adjusting data-field mappings

Every metric is defined in a small dict near the top of `exporter.py`:

```python
GRID_FIELDS: dict = {
    "solax_grid_voltage_volts": {
        "index": 0,      # ← change this to remap the reading
        "scale": 0.1,    # ← raw × scale = real value
        "unit": "V",
        "description": "Grid / Mains Voltage",
    },
    ...
}
```

To change which `Data[]` index maps to which metric, just edit `"index"`.  
To change the unit conversion, edit `"scale"`.

---

## Prometheus scrape config example

```yaml
scrape_configs:
  - job_name: solax
    static_configs:
      - targets: ["localhost:9101"]
```

---

## Grafana dashboard ideas

- Current solar output vs. house consumption vs. grid feed-in
- Daily / lifetime energy totals
- Inverter temperature over time
- Multi-inverter comparison (use the `inverter` label to split series)
