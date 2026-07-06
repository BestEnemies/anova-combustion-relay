# Anova Oven to Combustion Relay — ESP32 (MicroPython)

Bridges an Anova Precision Oven's live temperature into the Combustion app by
presenting as a Combustion Gauge-class node over BLE. 


The gauge serial is derived from the chip MAC.


## Setup instructions

### 1. What you need

- An **ESP32-S3** or an **ESP32 with PSRAM** (e.g. LOLIN D32 Pro). The Anova
  WiFi + TLS bridge needs the hardware crypto of the S3 or the extra RAM of a
  PSRAM board.
- An **Anova Precision Oven** on your Anova account, and an Anova **Personal
  Access Token**.
- A host PC with **Python 3** and a USB cable.

### 2. Install the host tools

```
python -m pip install esptool mpremote pyserial
```

Find the board's serial port: `python -m serial.tools.list_ports`.

### 3. Flash MicroPython

Download the firmware for your board from
[micropython.org/download](https://micropython.org/download/) (v1.28.0+):

| Board | Firmware variant | Flash offset |
|---|---|---|
| ESP32-S3 | `ESP32_GENERIC_S3` | `0x0` |
| ESP32 + PSRAM (WROVER / D32 Pro) | `ESP32_GENERIC-SPIRAM` | `0x1000` |
| plain ESP32 (BLE gauge only) | `ESP32_GENERIC` | `0x1000` |

```
python -m esptool --port <PORT> erase_flash
python -m esptool --port <PORT> --baud 460800 write_flash <OFFSET> <firmware.bin>
```


### 4. Get your Anova token

Sign in at [developer.anovaculinary.com](https://developer.anovaculinary.com)
and create a **Personal Access Token** (the `anova-...` string).

### 5. Configure

Optional: you can do this after upload in the web ui.

Copy `sample_config.json` to `config.json` and edit it (2.4 GHz WiFi + your
PAT; the rest are sensible defaults). `config.json` is git-ignored because it
holds your WiFi password and Anova token.

```json
{
  "wifi_ssid": "your-2.4GHz-ssid",
  "wifi_password": "...",
  "anova_pat": "anova-...",
  "mdns_hostname": "anovaRelay",
  "follow_oven": true,
  "broadcast_mode": "always",
  "broadcast_grace_min": 10,
  "relay_probes": false,
  "relay_connect": false
}
```

### 6. Deploy the code

From the repo directory, copy **all** the modules plus your config to the board:

```
python -m mpremote connect <PORT> fs cp protocol.py gauge.py ble.py anova.py web.py main.py config.json :
```

### 7. First run

Reset the board (or `python -m mpremote connect <PORT> reset`). `main.py`
auto-starts on boot: it connects to WiFi, starts the Anova bridge, and serves
the web UI. After ~15 s open **http://anovarelay.local/** (or the IP printed
on the serial console: `[web] config UI at ...`).

- Watch/interact over serial: `python -m mpremote connect <PORT> repl`, then
  type a temperature (`110`, `225f`) or `help`.
- Re-deploying a single file after an edit:
  `python -m mpremote connect <PORT> fs cp web.py :` (the board reboots).

## Files

| File | Purpose |
|---|---|
| `protocol.py` | Wire formats |
| `gauge.py` | Device state machine: logs, alarms, request handling |
| `ble.py` | BLE transport: advertising set + NUS GATT server, MeatNet relay + central-role probe link (low-level `bluetooth` API) |
| `anova.py` | Anova cloud client (WebSocket over TLS), runs in its own thread |
| `web.py` | Web UI: HTTP + WebSocket server (status/log stream, config, control) |
| `main.py` | Auto-starting console + 1 Hz tick/notify loop; wires everything together |
| `config.json` | WiFi, Anova PAT, and feature settings (deployed to the board) |



## Web UI (config + control)

When WiFi is up, the device serves a config/control panel on port 80 —
open `http://anovarelay.local/` from any browser or phone on the same network
(the IP is printed on the serial console at boot: `[web] config UI at ...`).

- **Live status + log** 
- **Control** follow-oven toggle, manual temperature,
  sensor-present / low-battery / overheating flags, high/low alarms, and
  **broadcast mode**.
- **Configuration** (saved to `config.json`; reboot to apply): WiFi
  credentials, gauge serial, Anova Personal Access Token, mDNS hostname,
  follow-on-boot. "Save & reboot" persists and restarts the device.


### Broadcast mode

- **Always** (default): the gauge always advertises over BLE.
- **Only while oven is on**: the gauge broadcasts only when the oven is
  actively cooking (`state.state.mode != "idle"`), and for a configurable
  **grace period** (minutes) after it turns off, then stops advertising and
  disconnects any client. It resumes automatically when the oven turns on
  again. Useful to keep the device silent/idle between cooks.


### MeatNet probe relay

Optional (toggle in the web UI Control section, persisted as `relay_probes`).
When on, the device scans for nearby Combustion **probes** advertising
directly (product type 1) and re-broadcasts each one as a repeated MeatNet
**node** advertisement (product type 2) with the hop-count byte set, so the
probe's live temperature reaches the app *through* this relay when the probe
is out of the phone's range. 