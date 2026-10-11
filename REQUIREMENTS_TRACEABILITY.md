# Requirements Traceability — Alpha Version vs. Approved IPJ517C Scope

**Approved scope document:** *Design of an Edge-Based Vehicle Theft Detection System using a LoRa-GSM Hybrid Network*, M.M. Mabaso (221000343), Supervisor: Dr. P Phoobane, Central University of Technology, Free State.

This document traces every element of the approved proposal's Objective 3 (the objective this Alpha demonstration addresses) to the feature that implements it in the current codebase, with pointers to where each can be verified. Objectives 1 and 2 (literature review, stakeholder survey) are separate research-methodology phases of the dissertation and are not in scope for a *system* Alpha demonstration — only RO3's prototype is.

## 1. Scope in force for this Alpha

> **RO3:** *"Design and evaluate a prototype edge-based vehicle theft detection framework utilizing a mobile node, an edge gateway running an anomaly detection algorithm, and a hybrid LoRa-GSM communication layer to measure detection responsiveness and communication failover performance."*

Everything below is scoped to RO3 and the "System Prototype" section (proposal pages 15–19, Figures 4–9).

## 2. Component-by-component traceability

| Approved proposal element | Implemented as | Evidence | Status |
|---|---|---|---|
| ESP32 mobile node | ESP32 firmware, deep-sleep architecture | `01_Mobile_Node_ESP32/esp32_mobile_node/esp32_mobile_node.ino` | **Implemented** |
| SW-420 vibration sensor as low-power digital interrupt trigger | Accelerometer (MPU6050/MPU6500, auto-detected) motion interrupt on I2C, wakes ESP32 from deep sleep | README §2 (Mobile Node); `esp32_mobile_node.ino` `configureMpuMotionInterrupt()`; TESTING_EVIDENCE.md defect B1 | **Implemented — component substitution, see §3.1** |
| Neo-6M GPS module for geospatial data | Neo-6M + TinyGPSPlus, bounded timeout, RTC-persisted last-fix fallback | README §2 (Mobile Node) | **Implemented** |
| SX1276 LoRa transceiver, SPI, ESP32 → Pi 5 | SX1278 LoRa transceiver, SPI, 433 MHz | README §2; `Phase1_Hardware_Wiring.md`; TESTING_EVIDENCE.md defect B2 | **Implemented — component substitution, see §3.2** |
| Telemetry: latitude, longitude, speed, heading | `TelemetryPacket` struct: lat, lon, speed, heading **plus** satellites, motion flag, GPS-validity flag, battery mV, uptime | `esp32_mobile_node.ino` `TelemetryPacket`; TESTING_EVIDENCE.md test A1 | **Implemented, exceeds proposal** |
| Raspberry Pi 5 edge gateway, local anomaly detection, no cloud dependency | Pi 5 gateway (`pi_gateway.py`), fully local, no external network call in the detection path | README §2 (Edge Gateway) | **Implemented** |
| Isolation Forest unsupervised anomaly detection on incoming telemetry, no pre-labelled theft data required | Per-vehicle `IsolationForest` (scikit-learn), rolling feature window, fits/refits on rolling history, cold-start fallback before enough baseline data exists | `pi_gateway.py` `NodeAnomalyDetector`; TESTING_EVIDENCE.md tests A3–A4 | **Implemented** |
| SIM800L GSM module, UART, emergency failover, activates only on anomaly confirmation | SIM800C GSM module, UART, same failover-only activation policy, with per-vehicle alert cooldown | README §2; `sim800_gsm.py`; TESTING_EVIDENCE.md defect B3 | **Implemented — component substitution, see §3.3** |
| SMS alert to vehicle owner/monitoring personnel with GPS coordinates, speed, timestamp | SMS via SIM800C with Google Maps link, speed, timestamp; alert also logged to DB regardless of SMS delivery success | `pi_gateway.py` `_dispatch_alert()`; README §2 | **Implemented** |
| SMS alerts / **mobile app notifications** / **direct phone communication** (proposal lists three channels) | SMS only | README §4 Known limitations | **Partial — app/voice channels deferred, SMS is the implemented channel** |
| Live telemetry dashboard showing LoRa link status, GSM standby status, anomaly score | Live web dashboard (FastAPI + Leaflet + WebSocket): colour/status-coded vehicle marker (green/amber/red), live fleet sidebar with signal (RSSI) and battery, alert timeline | README §2 (Dashboard) | **Implemented** |
| Anomaly score thresholding (proposal mockup: <0.10 normal, >0.58 anomaly, fixed cutoffs) | scikit-learn `IsolationForest` native `decision_function` score with `contamination=0.05`-driven automatic thresholding (not a fixed illustrative cutoff) | `pi_gateway.py` `NodeAnomalyDetector.score()`; TESTING_EVIDENCE.md tests A3–A4 | **Implemented — refined from illustrative proposal figures, see §3.4** |
| Route visualisation: blue trail (authorised route) vs. red trail (unauthorised/deviated route) after a theft event (proposal Figure 9) | Single-colour live trail per vehicle; anomaly state is shown on the vehicle marker (red ring) and in the alert timeline, not yet as a two-colour trail | README §4 Known limitations | **Deferred, see §4** |
| Detection responsiveness / communication failover performance measurement (RO3's stated evaluation target) | End-to-end pipeline works and is functionally tested. Packet Delivery Ratio (PDR) is now measured automatically from per-packet sequence numbers (dashboard Analytics page). Formal alert-latency and failover-success-rate measurement from a structured field test not yet collected | `dashboard/server.py` `_packet_delivery_ratio()`; README §8 Next steps | **Partial — PDR implemented; latency/failover field test deferred, see §4** |
| Communication failover when the vehicle is unreachable (implied by the LoRa-GSM hybrid design) | Loss-of-contact watchdog: a vehicle silent for longer than a configurable timeout (default 16 min = 3 missed heartbeats) triggers an SMS with its last known position. Alerts are queued and retried if the GSM module is temporarily unavailable | `gateway/pi_gateway.py` `_check_contact()`, `AlertDispatcher`; TESTING_EVIDENCE A8, A13 | **Implemented** |

## 3. Explicit scope changes and justification

### 3.1 SW-420 vibration sensor → accelerometer-based motion interrupt
The proposal specifies a discrete SW-420 vibration switch. The build uses an I2C accelerometer (MPU6050/MPU6500) instead, wired to the same low-power hardware-interrupt role: it holds the ESP32 in deep sleep and only wakes it on a physical disturbance, exactly matching the proposal's stated purpose ("keeping the node in deep-sleep mode until a physical disturbance is detected"). The change gives axis-resolved motion data (rather than a binary switch trip) and — as an unplanned but genuine finding — the specific board obtained turned out to be a pin-compatible MPU6500 rather than a true MPU6050, requiring dual-silicon register-scheme detection (TESTING_EVIDENCE.md, defect B1). This is documented as a hardware-availability substitution, not a scope reduction: the functional requirement (low-power interrupt-driven wake on disturbance) is fully met.

### 3.2 SX1276 → SX1278, 868 MHz → 433 MHz
Both are pin- and protocol-compatible SX127x-family LoRa transceivers from the same vendor family; the substitution is a frequency-band/part-availability decision (433 MHz is the ISM band used here) and does not change the architecture, the SPI interface, or the modem parameters (SF9/BW125kHz/CR4-5) targeted. The Pi-side receive driver (`sx127x_lora.py`) was written from scratch and cross-referenced against the exact register sequence the ESP32-side LoRa library produces, specifically to guarantee protocol compatibility across this substitution (TESTING_EVIDENCE.md, defect B2).

### 3.3 SIM800L → SIM800C
Same substitution class as §3.2: both are AT-command-compatible GSM/GPRS modules from the same manufacturer family, wired via UART. The SIM800C was used for supply/reliability reasons; the driver (`sim800_gsm.py`) implements the identical failover-only activation policy the proposal specifies. Ubuntu-on-Pi5's UART device-naming differences (not the module substitution itself) required adding serial-port auto-detection (TESTING_EVIDENCE.md, defect B3).

### 3.4 Anomaly scoring: fixed illustrative cutoffs → model-native contamination-based thresholding
The proposal's system-prototype figures illustrate normal/anomalous behaviour with fixed example scores (below 0.10 / above 0.58). The implemented system uses scikit-learn's `IsolationForest` directly: `decision_function()` output combined with the model's own `contamination=0.05` parameter to set the anomaly boundary automatically per vehicle, rather than a manually fixed cutoff. This is a refinement consistent with the proposal's stated algorithm (Isolation Forest, unsupervised, no pre-labelled theft data) — the specific numeric example in the proposal's mockup figures was illustrative, not a literal specification.

## 4. Deferred items, and why

| Deferred item | Reason | Planned resolution |
|---|---|---|
| Blue/red two-colour route trail (authorised vs. deviated) | Visual polish item; the same information (anomaly state) is already surfaced via marker colour and the alert timeline, so no detection capability is missing — only this specific chart-styling detail | Straightforward addition to the dashboard once prioritised (split the live trail into segments coloured by each point's `is_anomaly` flag) |
| Alert latency, Packet Delivery Ratio (PDR), failover success rate — the exact metrics RO3 specifies for evaluation | Requires a structured field/range test campaign (multiple runs, physical separation, logged timestamps at each pipeline stage) that has not yet been scheduled; the pipeline itself is functionally verified (TESTING_EVIDENCE.md §A–B) but not yet benchmarked against these specific metrics | Scheduled as README §7 Next steps item 1–2; this is the natural next phase between Alpha and Beta |
| Mobile app notifications / direct phone call as alternate alert channels | Proposal lists SMS as one of three possible channels; SMS was prioritised as the primary, testable channel for the Alpha | Out of scope for Alpha; would be a Beta-stage addition if prioritised |
| AES encryption of the LoRa payload | Not part of the original RO3 prototype description; added independently as a security consideration once the confidentiality gap was identified during implementation | README §5 Ethical/privacy/security considerations; README §7 Next steps item 4 |
| Empirical survey (Objective 2) | Separate research-methodology phase of the dissertation (stakeholder questionnaire), not part of the system prototype this Alpha demonstrates | Out of scope for this assignment by design — RO2 is evaluated separately from RO3 |

## 5. Summary

Every mandatory RO3 component — mobile node, low-power motion-interrupt wake, GPS telemetry, LoRa link, edge-resident Isolation Forest, GSM failover-only SMS alerting, and a live dashboard — is implemented and independently verified (see TESTING_EVIDENCE.md). Three part-substitutions (accelerometer for the SW-420, SX1278/433 MHz for SX1276/868 MHz, SIM800C for SIM800L) are hardware-availability decisions that preserve the exact functional role each component plays in the approved architecture, and are documented above rather than left unexplained. The two genuinely deferred items — formal alert-latency/PDR/failover-rate benchmarking and the two-colour route trail — are explicitly scoped as next steps, not gaps discovered late.
