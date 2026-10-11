# Testing Evidence — Alpha Version

This log covers two categories of testing conducted during development: (A) automated/scripted logic tests run against the Pi gateway's parsing and anomaly-detection code, with captured command output; and (B) hardware/field defects found and resolved during physical bring-up and integration of the ESP32 node, the LoRa link, the GSM module, and deployment. Both are included because a working IoT system depends on correctness at both levels — the algorithm has to be right, and the hardware has to actually talk to itself.

---

## A. Automated logic tests (Pi gateway)

### A1. Telemetry packet parsing round-trip

**Input:** A synthetic packet built with Python's `struct.pack` using the exact same field layout as the ESP32's `TelemetryPacket` struct (`node_id=1001, lat=-26.204103, lon=28.047305, speed=45.2, heading=180.0, satellites=8, motion=0, gpsValid=1, battery=4100mV, uptime=120s`).

**Expected:** `parse_telemetry_packet()` returns a `Telemetry` object with all fields matching the input within floating-point precision.

**Actual result (PASS):**
```
Packet size: 29
Parse round-trip OK: Telemetry(node_id=1001, latitude=-26.204103469848633,
longitude=28.047304153442383, speed_kmph=45.20000076293945, heading_deg=180.0,
satellites=8, motion_flag=False, gps_valid=True, battery_millivolts=4100,
uptime_seconds=120, rssi_dbm=-70, snr_db=8.5, ...)
```
Confirms the 29-byte struct format (`<IffffBBBHI`) matches the ESP32's packed C struct exactly, byte for byte.

*Update (packet v2):* the packet is now 31 bytes (`<BBIIffffBBBH`: protocol version, packet type, node ID, sequence number, then the same position/motion/battery fields; the meaningless `uptimeSeconds` was dropped). The firmware enforces the size with `static_assert(sizeof(TelemetryPacket) == 31)`, and the gateway still accepts 29-byte v1 packets from un-reflashed nodes (test A12).

### A2. Malformed/oversized packet handling

**Input:** The same valid packet with one extra trailing byte appended (30 bytes instead of 29) — simulating a corrupted or non-telemetry LoRa packet arriving on the same frequency.

**Expected:** The parser should reject it and return `None` rather than raising an exception or unpacking garbage.

**Actual result (PASS):**
```
[WARNING] Dropping packet with unexpected size 30 bytes (expected 29) — not a valid TelemetryPacket.
Oversized packet correctly dropped.
```

### A3. Isolation Forest — baseline (no false positives under normal commute pattern)

**Input:** 60 simulated telemetry samples representing a straight-line commute (steady ~0.02km steps north-east, speed 40±5 km/h, heading 90°±3°, no motion flag, GPS always valid).

**Expected:** With `ISOLATION_FOREST_CONTAMINATION = 0.05`, roughly 5% of "normal" samples may still be flagged (this is an inherent property of the contamination parameter, not a bug) — a low, bounded false-positive rate, not runaway flagging.

**Actual result (PASS):**
```
Alerts during 60 'normal' samples (expect 0 or very few): 3
```
3/60 = 5.0%, matching the configured contamination parameter exactly.

### A4. Isolation Forest — true anomaly detection

**Input:** One clearly anomalous sample injected after the 60 baseline samples: a 5° lat/lon jump (~550km), speed 180 km/h, motion flag set, degraded RSSI/SNR.

**Expected:** `is_anomaly = True`, reason `isolation_forest`.

**Actual result (PASS):**
```
Outlier result: {'is_anomaly': True, 'reason': 'isolation_forest', 'score': -0.0637}
```

### A5. Live end-to-end push (SQLite → FastAPI → WebSocket)

**Input:** A fresh telemetry row and alert row inserted directly into the shared SQLite database by an external process, simulating what the gateway does on packet receipt, while a WebSocket client is connected to the dashboard.

**Expected:** The dashboard backend's change-poller detects the new rows within one polling interval (≤1.5s) and pushes both to the connected client.

**Actual result (PASS):**
```
snapshot received, latest count = 1
pushed message type: telemetry -> node 1001
pushed message type: alert -> node 1001
LIVE PUSH TEST PASSED
```

### A6–A15. Code-review fixes — scripted gateway regression run

After a full code review, the gateway, GSM driver, database layer and dashboard were revised (see B6–B8 and the README). The run below drove the real `pi_gateway.py` logic end to end: the SX1278 and SIM800C were replaced with software fakes, and packets were fed straight into the processing path. Every SMS the fake modem received was checked against the 160-character limit.

| # | Scenario | Expected | Actual result |
|---|---|---|---|
| A6 | Database created by the old version (with `uptime_seconds`, 30 rows) opened by the new code, twice. | Schema migrated once, no rows lost. | PASS — `migration OK: 30 rows kept` |
| A7 | Packet from a node ID not in `known_node_ids`. | Ignored. | PASS — `Ignoring packet from unknown node 2002` |
| A8 | Motion packet with no GPS fix ever, while the SIM800C is **not** responding. The modem then comes back. | Alert recorded immediately, SMS queued, then delivered after automatic reconnect; `sms_sent` flipped to 1. | PASS — alert queued; `Trying to reconnect to the SIM800C (2 alert(s) waiting)... SIM800C ready`; SMS delivered (92 chars) |
| A9 | Fresh GPS fix arrives after an alert that only had a stale position. | One `LOCATION UPDATE` SMS. | PASS — 100 chars, link to the new fix |
| A10 | Duplicate sequence number; sequence jumping backwards (seq 20 after 39); sequence restarting at 1. | Duplicate and replay dropped; restart accepted. | PASS — 37 + 1 rows stored, as expected |
| A11 | Sequence jumps 1 → 5. | 3 missed packets counted (feeds PDR). | PASS — `missed packets counted: 3` |
| A12 | Legacy 29-byte v1 packet; 10-byte garbage packet; packet containing NaN coordinates. | v1 accepted; garbage and NaN rejected. | PASS |
| A13 | Vehicle silent for 17 minutes; watchdog runs twice. | Exactly one `CONTACT LOST` SMS; cleared by the next packet. | PASS — sent once (135 chars); `contact restored on next packet` |
| A14 | Worst-case alert text (5-digit speed, 3-digit negative longitude, date-stamped stale fix). | ≤ 160 characters. | PASS — 156 chars |
| A15 | Gateway restarted on the same database. | Model history and last sequence number restored (no cold start, duplicates still caught). | PASS — `restart restored last sequence 6 and 42 samples` |

Sample SMS texts produced during the run:

```
THEFT ALERT vehicle 3003          LOCATION UPDATE vehicle 3003        CONTACT LOST vehicle 3003
Motion detected (no baseline)     GPS fix 19:15, 42km/h               No signal for 17 min - out of range or jammed?
No GPS fix yet                    https://maps.google.com/?q=...      Last fix 19:17
Batt 4.10V RSSI -40dBm                                                https://maps.google.com/?q=...
```

**Dashboard state logic (A16).** The shared `vehicleState()` function was run under Node against nine scenarios. All passed: a healthy vehicle; a motion packet before GPS lock with a recent fix (stays *live*); never had a fix; fix 20 minutes old; silent for 20 minutes; theft alert 5 minutes ago followed by a normal packet (stays *theft alert*); theft alert 40 minutes ago (expired → *live*); contact lost and still silent; contact lost then reported again. A follow-up location update is correctly not treated as a threat.

**SMS confirmation push (A17).** With a WebSocket client connected, an alert row was inserted with `sms_sent = 0`, then marked sent 2.5 s later. The client received `('alert', 5, 0)` followed by `('alert_update', 5, 1)`, so the dashboard changes from "SMS pending" to "SMS sent" without a refresh.

**Gateway heartbeat (A18).** The gateway wrote its `gateway_status` row, and `/api/system/status` reported `running: True`, `gsm_ready: 0`, `alert_numbers: 1`, `seconds_since_heartbeat: 4`.

The dashboard was then started against the resulting database: all six pages and the API returned HTTP 200, the WebSocket snapshot delivered 2 vehicles and 4 alerts, `/api/analytics/summary` returned a Packet Delivery Ratio per vehicle, and every page script passed `node --check`. The revised ESP32 firmware passed a host-side `g++ -std=gnu++17 -Wall -Wextra` syntax/type check against stubbed Arduino headers, including the `static_assert` on the 31-byte packet. The ESP32 toolchain couldn't be downloaded in the test environment, so a real Arduino IDE compile and flash is still needed.

---

## B. Hardware/field defects found and resolved

| # | Scenario | Expected | Actual (defect) | Root cause | Resolution |
|---|---|---|---|---|---|
| B1 | MPU6050 motion interrupt should fire GPIO27 HIGH and wake the ESP32 from deep sleep when the board is shaken. | `[BOOT] Wake reason: EXT0 (MPU6050 motion interrupt)` on the next boot after shaking. | Accelerometer readings were correct (raw values tracked physical shaking, ~1g magnitude at rest) but the interrupt never fired — `INT_PIN` stayed LOW through vigorous, sustained shaking. | Diagnostic register readback revealed `WHO_AM_I = 0x70`, not the expected `0x68` — the board carries an **MPU6500** die, not a true MPU6050. The MPU6500 uses a completely different "Wake-on-Motion" register scheme (`WOM_THR`/`LP_ACCEL_ODR`/`ACCEL_INTEL_CTRL`) rather than the legacy MPU6050 comparator (`MOT_THR`/`MOT_DUR`/`MOT_DETECT_CTRL`), despite sharing the same I2C address and accel/gyro output registers. | Firmware now detects the chip variant from `WHO_AM_I` at boot and configures whichever register scheme matches the silicon actually present. Confirmed working after fix (user-reported, live shake test passed). |
| B2 | LoRa packets sent by the ESP32 (Sandeep Mistry "LoRa" library defaults: SF9, BW125kHz, CR4/5, sync word 0x12) should be received by the Pi's from-scratch SX1278 driver. | Pi gateway logs a received packet with valid CRC. | *(Preemptively addressed, not found live)* — the Pi-side driver was built by cross-referencing the exact register values the Arduino library produces (including the easy-to-miss detail that it never calls `setSyncWord()`, so the sync word stays at the chip's power-on default of `0x12`), rather than assuming standard values, specifically to avoid a silent protocol mismatch. | N/A — preventative design decision based on direct source inspection of `arduino-LoRa`'s `LoRa.cpp`. | Verified via struct-size and round-trip parsing tests (A1); full live RF test with physical separation is a next step (see README §6). |
| B3 | GSM module should be found and respond to `AT` on `/dev/serial0`. | `SIM800C responding at 9600 baud on /dev/serial0.` | `[ERROR] Could not open /dev/serial0: [Errno 2] No such file or directory` — `gsm_ready` stayed `False`, so every alert silently skipped the SMS-send step entirely (logged, not sent). | Ubuntu-on-Pi5 doesn't always ship the same udev rule Raspberry Pi OS uses to symlink `/dev/serial0`, and the Pi 5's RP1 chip enumerates UARTs differently to older Pis — the real device could be `/dev/ttyAMA0`, `/dev/ttyAMA10`, etc. | `sim800_gsm.py` now auto-detects the serial port across a candidate list (`/dev/serial0`, `/dev/ttyAMA0`, `/dev/ttyAMA10`, `/dev/ttyS0`, `/dev/ttyUSB0/1`), the same way it already auto-detected baud rate. Verified with a scripted test simulating the missing-port + present-port scenario (port scan correctly falls through to the working candidate). |
| B4 | Running `python3 pi_gateway.py` manually should initialize the LoRa radio's GPIO pins without error. | Radio initializes; gateway proceeds to listen. | `lgpio.error: 'GPIO busy'` on `LORA_RESET_PIN`. | The `vehicle-gateway.service` systemd unit was already running in the background (enabled from an earlier setup step) and had already claimed the same GPIO lines — two instances of the same program were competing for the same hardware pins. | Diagnosed via `systemctl status vehicle-gateway.service` (confirmed `active (running)` at the time of the conflicting manual run, per its own timestamped log). Documented as a standing operational rule: check `systemctl status` for the relevant service before any manual run, to avoid the same conflict recurring. |
| B5 | Starting the dashboard manually (`uvicorn ... --port 8000`) should bind port 8000 and serve immediately. | `Uvicorn running on http://0.0.0.0:8000`. | `[Errno 98] error while attempting to bind on address ('0.0.0.0', 8000): address already in use`. | Same class of issue as B4 — `vehicle-dashboard.service` was already running and holding the port. | Same resolution pattern: check `systemctl status vehicle-dashboard.service` before manual runs; stop the service first if a manual/debug session is needed. |
| B6 | After fixing the wiring, `pi_gateway.py` should detect the SIM800C, which answers `AT` correctly in minicom on `/dev/ttyAMA0 @ 9600`. | `SIM800C responding at 9600 baud on /dev/ttyAMA0.` | `SIM800C did not respond to AT on any of [...]` — every port/baud combination failed, even though minicom proved the module registered on the network (`+CGREG: 0,1`, `+CSQ: 24,0`). | `ls -l /dev/ttyAMA0` showed the port owned by group `dialout` with mode `crw-rw----`, and `groups` showed the user was **not** in `dialout`. pyserial raised "Permission denied", but the driver logged open failures at DEBUG level, so they looked identical to "opened but no reply". | `sudo usermod -aG dialout $USER` and re-login. The driver now logs open failures at WARNING with a specific hint (permission denied → dialout; busy → another program holds the port), and stops trying other baud rates on a port that can't be opened. |
| B7 | An alert SMS should be delivered once the modem is working. | One SMS per alert. | *(Found in code review, before it could fail live)* — the alert text in the gateway log was ~200 characters. | A text-mode SMS on the SIM800C is limited to 160 GSM characters; `AT+CMGS` either errors or the message is truncated. | Alert texts redesigned and capped at 160 characters (worst case measured at 156, test A14); `send_sms()` also truncates defensively. |
| B8 | An alert raised while GPS is stale should point to the vehicle's location. | Maps link to the last known position. | The log showed `GPS: 0.000000,0.000000 (STALE)` with a Maps link to 0,0 (Gulf of Guinea). | The alert used the current packet's coordinates, which are 0,0 when the node has never had a fix in that boot. | Gateway tracks each vehicle's last valid fix and uses it in the alert ("Last fix HH:MM (stale)"), or says "No GPS fix yet"; a follow-up `LOCATION UPDATE` SMS is sent when a fresh fix arrives (test A9). |
---

## Summary

Of the defects found (B1–B8), all were root-caused and resolved, not merely worked around. B1 in particular is worth highlighting for the demo: it wasn't a wiring or coding mistake, but a genuine hardware-identity mismatch between two pin-compatible chips with incompatible register maps for the exact feature (motion interrupt) the project depends on most — found through structured register-readback diagnostics rather than guesswork, and fixed with a general dual-silicon detection mechanism rather than a one-off patch.
