# Edge-Based Vehicle Theft Detection System — LoRa-GSM Hybrid Network

**IPJ527C: Advanced Research Project B — Assessment 3 (Beta Version Demonstration)**  
Melusi Mlungisi Mabaso (221000343) · Supervisor: Dr P. Phoobane · Central University of Technology, Free State

> **Testing without the hardware:** see [Testing the beta without the hardware](#testing-the-beta-without-the-hardware-for-assessors) below. One command runs the real gateway logic and dashboard with a simulated vehicle.

An IoT vehicle theft detection system combining an ESP32-based in-vehicle sensor node, a LoRa telemetry link, a Raspberry Pi 5 edge gateway running machine-learning-based anomaly detection, a SIM800C GSM failover channel for SMS alerts, and a live web dashboard for tracking and history.

---

## 1. System overview

```
[ESP32 Mobile Node]  --LoRa 433MHz-->  [Raspberry Pi 5 Edge Gateway]  --SMS-->  [Registered phone(s)]
  MPU6050/6500                           SX1278 receiver                          via SIM800C
  Neo-6M GPS                             Isolation Forest anomaly detection
  SX1278 transmitter                     SQLite telemetry/alert store
                                                    |
                                          [Live Dashboard, FastAPI + Leaflet]
                                          viewable from any browser on the LAN
```

The mobile node sleeps in deep sleep to conserve battery and wakes on either a hardware motion interrupt from the accelerometer or a periodic heartbeat timer. On a motion wake it immediately transmits a motion packet with its last known position, then acquires a fresh GPS fix and sends a second packet; on a timer wake it sends a single heartbeat. The Pi 5 gateway listens continuously, unpacks each packet, scores it against per-vehicle Isolation Forest models trained on that vehicle's own history, and — if the packet is judged anomalous — sends an SMS alert via the SIM800C module. It also watches for silence: a vehicle that stops reporting (driven out of LoRa range, radio jammed, node destroyed) triggers a loss-of-contact SMS with its last known position. Everything is stored in a shared SQLite database, which a FastAPI dashboard reads to show live vehicle positions, history, alerts, analytics and system health.

## 2. Functions implemented

### Mobile Node (ESP32) — `01_Mobile_Node_ESP32/esp32_mobile_node/esp32_mobile_node.ino`
- Hardware-interrupt motion wake from deep sleep, driven entirely by direct I2C register access (no external MPU6050 library dependency).
- **Automatic dual-silicon support**: detects at boot whether the board carries a genuine MPU6050 (legacy motion-comparator registers) or the commonly-substituted MPU6500 (Wake-on-Motion registers) via `WHO_AM_I`, and configures the correct register scheme for whichever is actually present.
- Periodic heartbeat wake (timer-based) as a secondary wake source, independent of motion.
- **Immediate motion report**: on a motion wake the node transmits straight away with its last known position, instead of first waiting up to 45 s for a GPS fix, then sends a follow-up packet with the fresh fix.
- GPS acquisition via Neo-6M/TinyGPSPlus, with a bounded timeout and fallback to the last known fix (persisted across sleep cycles in RTC memory) if no fresh fix is acquired in time.
- LoRa telemetry transmission (SX1278, 433 MHz, SF9/BW125kHz/CR4-5) as a fixed-size 31-byte packed binary struct (v2) carrying a protocol version, packet type and a per-packet **sequence number**.
- The SX1278 is put into LoRa sleep before the ESP32 enters deep sleep (otherwise it stays in standby drawing ~1.6 mA the whole time).
- Battery voltage monitoring via resistive divider, read with the ESP32's factory-calibrated `analogReadMilliVolts()` and averaged over 8 samples.
- Reliability: bounded retries with graceful degradation for both the motion sensor and the LoRa radio — a failed sensor or radio does not halt the node, it falls back to a reduced-functionality mode and keeps operating.

### Edge Gateway (Raspberry Pi 5) — `02_Edge_Gateway_RPi5/gateway/`
- `sx127x_lora.py`: a from-scratch SX1278 receive driver (no third-party LoRa library), written to be a protocol-exact match for the ESP32's LoRa library at the register level, with continuous reception via a DIO0 GPIO interrupt, thread-safe register access, and a periodic health check that re-arms the receiver if it ever drops out of receive mode.
- `sim800_gsm.py`: AT-command driver for the SIM800C, with automatic serial-port and baud-rate detection, clear diagnostics for ports that can't be opened (e.g. missing `dialout` permission), SIM-card and network-registration checks, reconnection, a 160-character single-SMS limit, and retried SMS sending with response verification.
- `telemetry_store.py`: SQLite persistence layer (WAL mode with graceful fallback) shared between the gateway and the dashboard, with an automatic, lossless schema migration from the v1 database.
- `pi_gateway.py`: orchestrates the above:
  - **Anomaly detection**: two per-vehicle **Isolation Forest** models. A movement model scores packets that carry a fresh GPS fix (speed, heading, speed implied by consecutive fixes, motion, time of day); a context model scores packets without one (motion and time of day only). Cyclic features use sine/cosine encoding, stale-GPS packets are kept out of the movement model, and history is rebuilt from the database on restart.
  - **Three alert types**: theft alert, follow-up location update, and **loss-of-contact** (vehicle silent for longer than a configurable timeout). All are kept under 160 characters and link to the last known location.
  - **Resilient delivery**: a dedicated worker thread owns the SIM800C. Alerts are recorded in the database immediately; if the modem is down, SMSes are queued and retried while the gateway reconnects to it, and packet processing is never blocked.
  - **Packet integrity**: sequence-number checks drop duplicates and naive replays and count missed packets. Packets from unknown node IDs or with invalid values are rejected.
  - Settings (alert numbers, serial port, contact timeout, known nodes) live in `gateway_config.json` rather than in code.

### Dashboard — `02_Edge_Gateway_RPi5/dashboard/`
- FastAPI backend (`server.py`) serving REST endpoints (latest position, history, alerts, fleet-wide analytics including **Packet Delivery Ratio** from the node's sequence numbers, system status) and a WebSocket that pushes new telemetry/alerts to connected clients in near real time, by polling the shared database independently of the gateway process.
- A real multi-page web application, server-rendered with Jinja2 (`templates/`), not a single HTML file: **Live Map** (animated status-coloured markers on a Leaflet map with switchable Dark / Streets / Satellite base maps from OpenStreetMap and Esri, no API key needed), **Fleet** (searchable vehicle table), **Vehicle Detail** (per-vehicle stats, map, alert history and history charts), **Alerts** (fleet-wide incident log), **Analytics** (aggregate speed/distance/anomaly stats per vehicle), and **System** (LoRa link health, DB size, connected clients). All six share one dark-theme layout and one WebSocket connection (`static/js/common.js`), with each page's own script subscribing only to the live-update types it needs.

## 3. How to run / access the system

Full setup instructions are in the phase documents at the project root and in `02_Edge_Gateway_RPi5/`:
- `Phase1_Hardware_Wiring.md` — pin-level wiring for both the ESP32 node and the Pi 5 gateway.
- `02_Edge_Gateway_RPi5/Phase3_Setup.md` — enabling SPI0/UART on Ubuntu, installing gateway dependencies, running `pi_gateway.py` (manually or as a systemd service).
- `02_Edge_Gateway_RPi5/Phase4_Dashboard_Setup.md` — installing and running the dashboard, viewing it from any device on the same network.

Quick reference, once hardware is wired and dependencies installed:

```bash
# ESP32: flash 01_Mobile_Node_ESP32/esp32_mobile_node/esp32_mobile_node.ino via Arduino IDE.

# Pi 5 gateway:
cd 02_Edge_Gateway_RPi5/gateway
source venv/bin/activate
python3 pi_gateway.py

# Pi 5 dashboard (separate terminal/process):
cd 02_Edge_Gateway_RPi5/dashboard
source venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8000
# then browse to http://<pi-ip-address>:8000/ from any device on the same network
```

### Testing the beta without the hardware (for assessors)

The physical system needs the ESP32 node, LoRa radios, a Raspberry Pi 5 and a SIM800C. To let anyone test it on an ordinary PC (Windows, macOS or Linux, Python 3.10+), `gateway/simulate.py` runs the **real gateway code and the real dashboard**. Only the vehicle node, the radio and the modem are simulated: packets are generated in the exact binary format the ESP32 sends, and SMS messages are printed to the console instead of being sent.

```bash
cd 02_Edge_Gateway_RPi5
python -m venv venv
venv\Scripts\activate            # Windows   (macOS/Linux: source venv/bin/activate)
pip install -r requirements-simulation.txt
cd gateway
python simulate.py
```

The browser opens the dashboard at `http://127.0.0.1:8000`. Keep it beside the console window. Over about 3 minutes, four scenarios play out:

| Scenario | What happens | What you should see |
|---|---|---|
| A. Parked | Routine heartbeats | Logged as "judged normal", no alert; vehicle green on the Live Map |
| B. Theft | The vehicle is moved at an hour it is never normally used, then driven away fast | **THEFT ALERT** SMS in the console; **LOCATION UPDATE** SMS once GPS has a fix; vehicle turns red; route drawn on the map; repeat alert after the (shortened) cooldown |
| C. Out of range | Packets stop | **CONTACT LOST** SMS with the last known position |
| D. Back in range | The node reports again | "contact restored"; the vehicle stays red because of the recent theft alert |

Before the scenarios, the simulation generates a week of normal history (parked, plus two daily trips) and trains both Isolation Forest models on it, so the alerts come from the trained models rather than the learning-period rule. Timings are shortened for the demo: 30 s SMS cooldown instead of 5 minutes, and a 20 s loss-of-contact timeout instead of 16 minutes. Afterwards, explore every dashboard page: Fleet → Vehicle 1001 (route and history), Alerts, Analytics (packet delivery ratio) and System (gateway/modem/radio health). The simulation uses its own database (`gateway/data/simulation.db`) and never touches real gateway data. Stop it with `Ctrl+C`.

There is no login system in this beta version — the dashboard is unauthenticated by design, intended for local-network access only during this stage (see Limitations below). No test login details apply.

## 4. Requirements checklist (traced to the approved IPJ517C scope)

Full component-by-component traceability, including justification for every hardware substitution, is in `REQUIREMENTS_TRACEABILITY.md`. Summary:

**Implemented:**
- ESP32 mobile node with deep-sleep, hardware-interrupt motion wake (accelerometer, substituted for the proposal's SW-420 — see traceability doc §3.1)
- Neo-6M GPS acquisition with timeout/fallback
- LoRa telemetry link, SX1278/433MHz (substituted for SX1276/868MHz — see traceability doc §3.2)
- Raspberry Pi 5 edge gateway with fully local (no-cloud) processing
- Isolation Forest anomaly detection, per-vehicle, unsupervised
- SIM800C GSM failover-only SMS alerting (substituted for SIM800L — see traceability doc §3.3), with queued retries and loss-of-contact alerts
- Live web dashboard: map, status-coded vehicle markers, fleet sidebar, alert timeline, history charts

**Partially implemented:**
- Alert delivery channel: SMS only, of the three channels (SMS/app notification/direct call) the proposal names
- Evaluation metrics: Packet Delivery Ratio is measured automatically from sequence numbers; alert latency and SMS failover were measured in a field test at the CUT stadium (9 October 2026), with a larger structured test planned

**Deferred (see traceability doc §4 for full reasoning):**
- Two-colour (authorised vs. deviated) route trail visualisation
- AES encryption/authentication of the LoRa payload
- Mobile app / direct-call alert channels

## 5. Known limitations and incomplete functions

- **No packet encryption yet.** LoRa telemetry is currently sent as plain (unencrypted) binary — a known, explicitly flagged gap rather than an oversight. Sequence numbers stop duplicates and naive replays, but they are not cryptographic: a forged packet with a higher sequence number would still be accepted. AES with an authentication tag on both the ESP32 and the Pi is the planned fix.
- **Single fixed gateway.** GSM lives on the gateway, not in the vehicle, so once a vehicle leaves LoRa range it can't be tracked. The loss-of-contact alert means that moment is reported rather than missed, but it also fires when the owner legitimately drives out of range. A GSM/LTE module in the vehicle itself is the production path.
- **2G-only GSM module.** The SIM800C uses 2G, which South African operators plan to switch off (the national target is the end of 2027). An LTE Cat-M1/NB-IoT module (e.g. SIM7080G) would replace it in a deployment.
- **GPS module is always powered.** The Neo-6M draws roughly 45 mA even while the ESP32 is in deep sleep, which dominates the node's battery budget. A MOSFET power switch or the module's UBX power-save mode would fix this.
- **No formal range/battery characterisation yet.** The system has been verified working end-to-end on the bench and over short-range testing, but structured range (RSSI/SNR vs. distance) and ESP32 deep-sleep battery-life measurements have not yet been collected.
- **Isolation Forest tuning is provisional.** The models train on each vehicle's own rolling history (about a week of packets) with 5% contamination, so roughly 5% of normal packets are still scored as outliers by design. They have not yet been evaluated against a substantial real-driving dataset, so false-positive/negative rates are not yet formally characterised.
- **Dashboard has no authentication.** Acceptable for a local-network beta demo; would need to be addressed before any real deployment.
- **Single-vehicle testing to date.** The system supports multiple `node_id`s architecturally (per-node models, per-node database rows), but has only been exercised with one physical mobile node so far.

## 6. Ethical, privacy and security considerations

- **Location data is sensitive personal data.** The system continuously records a vehicle's (and by extension, its owner's/driver's) real-time and historical location. This beta version has no data-retention policy or consent flow — for a real deployment, retention limits, owner consent, and access controls on the telemetry database would be required.
- **Dashboard has no authentication and no transport encryption (HTTP, not HTTPS).** Acceptable only because it is scoped to a trusted local network for this beta; anyone who can reach the Pi's IP on that network can view live vehicle positions. This must be resolved (auth + TLS) before any deployment outside a controlled demo environment.
- **LoRa telemetry is unencrypted.** A nearby attacker with a compatible SX127x radio could passively intercept location/speed data in transit (confidentiality risk), though spoofing a convincing forged packet would additionally require knowing the exact binary struct layout used. AES-128 on the payload is the planned mitigation (see Next steps).
- **False positives/negatives in anomaly detection carry real consequences.** An undetected theft (false negative) has an obvious safety/financial impact; an over-eager false positive triggers an unnecessary SMS alert and, in a scaled deployment, alert fatigue. The cold-start fallback and 5% contamination setting are conscious, documented trade-offs, not defaults left unexamined.
- **SMS alerts include the vehicle's exact coordinates as a Google Maps link.** Phone numbers and message content are handled only in memory/logs on the Pi; no third-party SMS gateway or cloud service is used, which limits (but does not eliminate) third-party data exposure.

## 7. Testing

See `TESTING_EVIDENCE.md` for a structured log of defects found and resolved during development (representative inputs, expected vs. actual results, root cause, and resolution), covering sensor bring-up, LoRa protocol compatibility, GSM connectivity, and deployment/process-conflict issues.

## 8. Next steps (post-beta)

1. Full end-to-end live test with the vehicle node physically separated from the gateway at realistic deployment range, capturing alert latency and failover success rate alongside the now-automatic Packet Delivery Ratio — the metrics specified in the approved scope's evaluation plan.
2. Structured LoRa range/RSSI/SNR logging and ESP32 battery-life measurement.
3. Collect real driving data to tune and evaluate the Isolation Forest models against actual false-positive/negative rates.
4. Add AES encryption with authentication (AES-GCM or HMAC) to the LoRa payload.
5. Two-colour (authorised vs. deviated) route trail on the dashboard map.
6. Move GSM/LTE into the vehicle node so tracking continues beyond LoRa range, using an LTE Cat-M1/NB-IoT module ahead of the 2G switch-off.

See `REQUIREMENTS_TRACEABILITY.md` for full traceability against the approved IPJ517C scope, including justification for every hardware substitution.
