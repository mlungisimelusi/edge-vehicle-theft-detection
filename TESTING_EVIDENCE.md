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

## C. Final-version features — automated tests (19 October 2026)

Encryption, arm/disarm, the two-colour route and the evaluation tool were added after the beta. The real gateway, firmware and dashboard code were tested as follows.

| # | Test | Result |
|---|---|---|
| C1 | **Firmware → gateway encryption compatibility.** The ESP32 firmware source itself (`esp32_mobile_node.ino` with `aes_gcm_lite.h`) was compiled on a PC with stand-ins for the Arduino libraries. It built a 51-byte packet with a random key, and the gateway's `lora_crypto.py` decrypted and verified it. | PASS — header `(3, 2, 1001, 42, 1)`: boot epoch 41 → 42 on a simulated power-on; position, speed and flags decoded exactly |
| C1b | **`aes_gcm_lite.h` correctness.** The firmware's own AES-128-GCM (written so the sketch doesn't depend on the ESP32 core's mbedtls headers, which were missing in the installed core: `mbedtls/gcm.h: No such file or directory`) was compared with Python's `cryptography` AESGCM on 500 random keys/nonces/data lengths (0–60 B additional data, 0–70 B plaintext), plus NIST SP 800-38D AES-128 test case 4. | PASS — 0 mismatches in 500; NIST tag `5bc94fbc…1a47` reproduced |
| C2 | AES-128-GCM: altered header byte, altered ciphertext byte, altered tag byte, wrong key | PASS — all four rejected as authentication failures |
| C3 | Replay of a previously accepted packet; forged packet; unencrypted v2 packet with `require_encryption` on | PASS — 3 rejected, counted on the System page; genuine packets still accepted |
| C4 | Node power-cycle (new boot epoch) vs. a packet from an older epoch | PASS — new epoch accepted, older epoch rejected as a replay |
| C5 | ARMED: heartbeat / motion | PASS — no alert / "Moved while ARMED" alert; armed motion not added to the learned baseline |
| C6 | DISARMED: motion while the model is learning; silence beyond the contact timeout | PASS — no alert (logged as owner use); no CONTACT LOST SMS while disarmed, but sent once armed |
| C7 | Database created by the beta version opened by the final version (twice) | PASS — new columns added, all rows kept |
| C8 | Alert timing columns | PASS — trigger → alert → SMS-sent timestamps recorded for every alert |
| C9 | Dashboard arm/disarm API | PASS — wrong PIN → HTTP 403; correct PIN → mode changed; 6th attempt after 5 wrong PINs → HTTP 429 lockout; disarming clears the vehicle's active alert |
| C10 | Two-colour route API | PASS — points after a theft alert flagged `deviated` (red) until the disarm, others blue |
| C11 | `eval_session.py` end to end (2 theft, 2 range, 1 normal, 1 shield session) | PASS — report showed 2/2 detected, 0/1 false alarms, PDR 100 % at 25 m and 83.3 % at 100 m (one packet deliberately dropped), CONTACT LOST 1/1 at 3.5 s, plus CSV files and charts |
| C12 | Full simulation (`simulate.py --quick`), all seven scenarios | PASS — see below |

**Simulation run (C12), alert sequence:** owner drive while disarmed → no alert (24 packets "driving judged normal", one isolated outlier not confirmed); theft while armed → `armed_motion` ×3 + `location_update`; out of range → `loss_of_contact`; attacks → 3 × `REJECTED`; unusual driving while disarmed → `isolation_forest`, confirmed on the 3rd outlier of 5. Route: 67 points, 33 red.

**Two design faults found by these tests and fixed** (they would have produced false alarms in real use):

| # | Fault | Root cause | Fix |
|---|---|---|---|
| C-F1 | The owner's ordinary drive raised an Isolation Forest alert. | The model was trained on all packets, so a few minutes of driving per day was "rare" next to thousands of parked heartbeats, and Isolation Forest scores rarity. | The model is now trained and applied **only on driving packets** recorded while disarmed. Whether the car should move at all is decided by arming. |
| C-F2 | Even then, about 1 driving packet in 20 was scored an outlier. | `contamination = 0.05` labels 5 % of normal data as outliers by design; alerting on a single packet would cause a false alarm on most drives. | **3-of-5 confirmation rule**: an ML alert needs 3 outliers among the last 5 driving packets. Real abnormal driving is confirmed within seconds (scenario G: on the 5th packet). |

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

---

## D. Isolation Forest benchmark (`ml_benchmark.py`, 22 October 2026)

The gateway's own detector (features, window, 3-of-5 rule) was trained on 14 days of synthetic owner trips (2 per day, 15 min, one packet per 10 s) and tested on 50 held-out normal trips and 50 trips of each anomaly type. Two profiles: **vehicle** (0–70 km/h) and **walking** (the field-test emulation, 3–6 km/h). Same data for every variant; seed 2026. Run: `python ml_benchmark.py --csv results`.

Trip-level results with the 3-of-5 rule (normal = false alarm rate per trip; others = detection rate):

| Profile | Variant | Normal (FA) | Speed | Night | GPS jump | Erratic | Packet F1 |
|---|---|---|---|---|---|---|---|
| vehicle | beta (c = 0.05) | 44 % | 100 % | 96 % | 4 % | 100 % | 0.793 |
| vehicle | tuned (c = 0.01) | 10 % | 48 % | 70 % | 0 % | 84 % | 0.402 |
| vehicle | **final** (c = 0.01 + envelope) | **10 %** | **100 %** | 70 % | **100 %** | **100 %** | 0.712 |
| walking | beta | 54 % | 100 % | 100 % | 10 % | 100 % | 0.868 |
| walking | tuned | 6 % | 100 % | 84 % | 0 % | 100 % | 0.793 |
| walking | **final** | **6 %** | **100 %** | 84 % | **100 %** | **100 %** | 0.825 |

**Two further design faults found and fixed:**

| # | Fault | Root cause | Fix |
|---|---|---|---|
| D-F1 | 44 % of normal trips raised a false alarm with the beta setting. | Consecutive packets are similar, so the 5 % outliers arrive in runs that satisfy 3-of-5. | Contamination lowered to 0.01. |
| D-F2 | With c = 0.01, a 110–160 km/h getaway was detected only 48 % of the time; a 3 km GPS jump never. | Isolation Forest splits only inside the training range, so values far beyond it land in the same leaf as the training maximum; a jump is a single packet and can never collect 3 votes. | Learned speed envelope (1.25 × owner's max speed; implied speed 1.5 × that) that alerts immediately. Simulation scenario G now alerts on the first packet (`speed_envelope`). |

Mean inference time: 6–9 ms per packet on the test PC (single packet through `NodeAnomalyDetector.evaluate`).

---

## E. Final field sessions (CUT stadium, 27 October 2026)

Gateway running the beta detector (contamination 0.05, no envelope); contact timeout 16 min; SIM800C disconnected (registration denied on the only 2G network, Vodacom 65501). Source: `eval_session.py report` and the gateway database.

| Measure | Result |
|---|---|
| Armed theft: first motion packet classified as theft | 4 / 4 |
| Armed theft: alert raised | 3 / 4 (4th started 2 min after the 3rd: held back by the 5-min cooldown, by design) |
| Packet received → alert raised (on the Pi) | mean 0.12 s, range 0.04–0.24 s |
| Normal disarmed walk (11.5 min) | 1 false alarm; 33 of 76 judged packets scored as outliers (model trained on one 20-min walk; hour-of-day features out of range) |
| Packet delivery ratio (66 min) | 98.9 % (656 / 663); 7 single-packet losses, all at RSSI ≤ −93 dBm |
| Mean RSSI by distance | 0–25 m −59 dBm, 25–50 m −97, 50–100 m −101, 100–137 m −104 (max distance 137 m) |
| Node powered off while armed | CONTACT LOST after 969 s (16-min timeout + watchdog); recovery 90 s after power-on |
| Metal tin at a few metres | did not block the link (packets still received) |
| SMS | 0 / 10 delivered (modem not registered); SMS delivery evidence from 9 October beta test (3 / 3) |

### D2. Validation split (`ml_validate.py`, 31 October 2026)

Settings were tuned on seed 1 only (grid: contamination 0.005/0.01/0.02/0.05 × envelope 1.1/1.25/1.5) and then tested on five unseen seeds (2–6), retraining on fresh owner data each time, 30 trips per type per seed. Mean ± SD over the five seeds:

| Profile / variant | Normal (FA) | Speed | Night | Jump | Erratic |
|---|---|---|---|---|---|
| Vehicle / baseline (c 0.05, no envelope) | 50 ± 14 % | 95 ± 9 % | 97 ± 4 % | 6 ± 3 % | 100 % |
| Vehicle / final (c 0.01, envelope 1.25) | 14 ± 7 % | 100 % | 79 ± 20 % | 100 % | 100 % |
| Walking / baseline | 42 ± 14 % | 100 % | 99 ± 1 % | 13 ± 7 % | 100 % |
| Walking / final | 12 ± 7 % | 100 % | 87 ± 11 % | 100 % | 100 % |

The tuning seed alone selected c = 0.005, envelope 1.1 (false alarms 5–8 %, night detection 67–73 % on unseen seeds). Field proportions are reported with 95 % Wilson intervals (e.g. 4/4 detections → 51–100 %; PDR 656/663 → 97.8–99.5 %).
