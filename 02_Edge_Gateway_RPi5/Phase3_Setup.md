# Phase 3 — Edge Gateway Setup (Raspberry Pi 5, Ubuntu Linux)

Covers enabling the hardware interfaces, installing dependencies, configuring the gateway, and running it — on **Ubuntu** (not Raspberry Pi OS) on a Pi 5.

---

## 1. Enable SPI0 and the UART

Ubuntu's official Raspberry Pi images use the same firmware/boot config as Raspberry Pi OS — `/boot/firmware/config.txt` — so the same overlays apply.

```bash
sudo nano /boot/firmware/config.txt
```

Add (or uncomment) these lines:

```
dtparam=spi=on
enable_uart=1
```

Then free up the primary UART from the Linux serial console (the SIM800C needs exclusive use of it), the same way as on Raspberry Pi OS:

```bash
sudo raspi-config
# Interface Options -> Serial Port
#   "Would you like a login shell to be accessible over serial?" -> No
#   "Would you like the serial port hardware to be enabled?"     -> Yes
```

If `raspi-config` isn't installed on your Ubuntu image, do it manually instead: ensure `console=serial0,115200` is **removed** from `/boot/firmware/cmdline.txt`, and confirm `enable_uart=1` is set as above.

Reboot:

```bash
sudo reboot
```

After reboot, confirm the devices exist:

```bash
ls -l /dev/spidev0.0        # SPI0, used by the SX1278
ls -l /dev/serial0          # UART, used by the SIM800C
ls /dev/gpiochip*           # GPIO chip(s) — lgpio/gpiozero auto-detect the right one
```

## 2. Permissions

Add your user to the groups that own these device nodes (names match Raspberry Pi OS's udev rules, which Ubuntu's Pi images also ship):

```bash
sudo usermod -aG spi,gpio,dialout $USER
```

Log out and back in (or reboot) for the group membership to take effect.

## 3. Install dependencies

```bash
cd 02_Edge_Gateway_RPi5
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

If you're not using a venv and Ubuntu blocks pip with an "externally managed environment" error (PEP 668, default since Ubuntu 23.04+), either use the venv above (recommended) or:

```bash
pip install -r requirements.txt --break-system-packages
```

`lgpio` compiles a small C extension on install — if it fails, install build tools first: `sudo apt install -y python3-dev build-essential`.

## 4. Configure the gateway

Settings live in `gateway/gateway_config.json` (no code changes needed):

```json
{
  "alert_phone_numbers": ["+27821234567"],
  "gsm_serial_port": "/dev/ttyAMA0",
  "contact_timeout_minutes": 16,
  "loss_of_contact_alerts": true,
  "known_node_ids": [1001],
  "require_encryption": true,
  "default_armed": true
}
```

- `alert_phone_numbers` — the number(s) that receive SMS alerts, in international format (`+27…`). Local format (`082…`) and placeholder numbers are rejected at startup with a clear log message. You can also pass them as an environment variable instead: `VTD_ALERT_NUMBERS="+27821234567,+27831234567"`.
- `gsm_serial_port` — the SIM800C's UART. `/dev/ttyAMA0` is what this Pi 5 uses (confirmed with minicom). Set it to `null` to auto-detect.
- `contact_timeout_minutes` — how long a vehicle may be silent before a **loss-of-contact** SMS is sent. The node sends a heartbeat every 5 minutes, so 16 minutes = 3 missed heartbeats plus a margin.
- `known_node_ids` — only packets from these node IDs are processed; anything else on 433 MHz is ignored. Leave the list empty to accept any node.
- `require_encryption` — reject every packet that isn't AES-encrypted and authenticated (recommended; older unencrypted firmware is then refused).
- `default_armed` — the mode of a vehicle that has never been armed or disarmed on the dashboard (`true` = protected from the start).

### 4a. Create the shared key and dashboard PIN (once)

The ESP32 and the gateway need the same AES-128 key. On the PC you flash the ESP32 from, in the project folder:

```bash
python 02_Edge_Gateway_RPi5/gateway/generate_secrets.py
```

This writes two files, both excluded from Git:
- `02_Edge_Gateway_RPi5/gateway/secrets.json` — the key and the dashboard **arm/disarm PIN**. **Copy it to the same folder on the Pi** (e.g. with WinSCP, or `scp`).
- `01_Mobile_Node_ESP32/esp32_mobile_node/lora_key.h` — the same key for the firmware. The sketch won't compile without it (the error message tells you to run this script).

Then flash the ESP32 again. If you ever create a new key (`--force`), you must re-copy `secrets.json` **and** reflash the ESP32, or the gateway will reject every packet as "authentication failed".

Install the new Python dependency on the Pi (once): `pip install -r requirements.txt` (adds `cryptography`).

The pin numbers at the top of `gateway/pi_gateway.py` and `gateway/sx127x_lora.py` are pre-set to the pin map in `Phase1_Hardware_Wiring.md` (LoRa RST=GPIO25, DIO0=GPIO24, SPI0 hardware CS on GPIO8/CE0).

## 5. Run it

```bash
cd gateway
python3 pi_gateway.py
```

Expected startup log (sample counts and times will differ on your Pi):

```
Loaded configuration from .../gateway/gateway_config.json
SQLite store ready at .../gateway/data/vehicle_theft_detection.db
Node 1001: restored history — 144 driving samples for the Isolation Forest (trained), last seen 22:25.
SX127x detected, REG_VERSION=0x12
SX127x configured: freq=433.000MHz SF=9 BW=125000Hz CR=4/5 sync=0x12 preamble=8
LoRa receiver armed — listening continuously on DIO0 (GPIO24) interrupt.
SIM800C responding at 9600 baud on /dev/ttyAMA0.
SIM800C ready on /dev/ttyAMA0, network registered, signal ~-65 dBm.
Gateway running — 1 alert number(s), loss-of-contact alerts ON (16 min), LoRa encryption AES-128-GCM (unencrypted packets rejected), new vehicles start ARMED. Listening for telemetry...
```

If you see `Could not open /dev/ttyAMA0 ... Permission denied`, your user isn't in the `dialout` group — run `sudo usermod -aG dialout $USER` and log out and back in (see §2). Don't use `sudo python3 pi_gateway.py` as a workaround: `sudo` drops your venv, so you'll get `No module named 'sklearn'`.

Every received packet is logged to the console and stored in the SQLite database (`gateway/data/vehicle_theft_detection.db`), which the dashboard reads. Alerts are written to the database the moment they're raised and then sent as SMS by a separate worker thread; if the SIM800C is unavailable, alerts are queued and retried every 30 seconds for up to 30 minutes, and the dashboard shows them as "SMS pending" until they go out.

Stop with `Ctrl+C` — SPI, GPIO, the serial port and the worker thread are all shut down cleanly.

## 6. Run automatically on boot (optional)

Create `/etc/systemd/system/vehicle-gateway.service`:

The service's `User=` must also be in the `dialout`, `spi` and `gpio` groups (§2), or the service will hit the same "Permission denied" on the SIM800C port.

```ini
[Unit]
Description=Vehicle Theft Detection Edge Gateway
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/path/to/02_Edge_Gateway_RPi5/gateway
ExecStart=/home/YOUR_USERNAME/path/to/02_Edge_Gateway_RPi5/venv/bin/python3 pi_gateway.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vehicle-gateway.service
journalctl -u vehicle-gateway.service -f   # tail the logs
```

## 7. Arm / disarm

Each vehicle is either **ARMED** or **DISARMED**, set with the button on its dashboard page (PIN from `secrets.json`):

| | ARMED (parked, owner away) | DISARMED (owner using the vehicle) |
|---|---|---|
| Any motion | **THEFT ALERT — Moved while ARMED**, immediately | no alert by itself |
| Isolation Forest | still runs (alerts on abnormal driving) | the **only** way a theft alert can be raised |
| Learned as "normal"? | never: thefts and tests stay out of the baseline | yes: the owner's driving is the baseline |
| Silence (no packets) | **CONTACT LOST** SMS | no SMS (the owner may have driven out of range) |

Disarming also acknowledges a theft alert: the vehicle stops showing red on the dashboard, and the red part of its route ends there.

## 8. Isolation Forest behaviour, in plain terms

Each vehicle has its own model, trained only on **its owner's driving**: packets with a fresh GPS fix while moving, recorded while disarmed. The features are speed, heading, the speed implied by the distance between consecutive fixes (catches position jumps such as towing or GPS spoofing), the motion flag, and time of day. Heading and time of day are encoded as sine/cosine pairs, so 23:00 is next to 00:00 and 359° is next to 1°.

Parked heartbeats are **not** judged by the model. Mixing thousands of parked heartbeats in made driving itself "rare", and Isolation Forest, which scores rarity, then flagged the owner's normal driving. Whether the vehicle should move at all is decided by arming it.

- **Learning period:** the model needs `MOVEMENT_MIN_SAMPLES` (100) driving packets, a few of the owner's trips, before it is trusted. Until then it reports "learning" and never alerts; arming the vehicle protects it meanwhile.
- **Confirmation rule:** with `contamination = 0.01`, about 1 in 100 perfectly normal packets is scored an outlier by design. An alert therefore needs **3 outliers in the last 5 driving packets** (`ML_CONFIRM_COUNT` / `ML_CONFIRM_WINDOW`). A genuinely abnormal drive is abnormal packet after packet, so it is still confirmed within seconds.
- **Learned speed envelope:** a packet faster than 1.25 × the fastest speed in the owner's history (or a position jump implying 1.5 × that) alerts **immediately** as `speed_envelope`. An Isolation Forest cannot rank values far outside its training range as more unusual than the extremes it was trained on, so this check catches getaways and GPS jumps the forest alone misses. Run `python ml_benchmark.py` to reproduce the comparison.
- **No self-judging:** the model refits every 5 new samples, and each packet is scored against the baseline fitted *before* it was added.
- **Restarts:** history is rebuilt from the database on startup. Motion recorded while armed is left out.
- **Visible verdicts:** every packet is logged with the reason it did or didn't alert (parked; learning; driving judged normal with its score; outlier 1 of 3 needed; armed motion).
- **SMS cooldown:** a 5-minute cooldown (`ALERT_COOLDOWN_SECONDS`) per vehicle limits repeated SMS while a theft continues. Every packet is still recorded and shown live.

## 9. The alert types

| SMS | When |
|---|---|
| `THEFT ALERT` — Moved while ARMED | Any motion while the vehicle is armed. |
| `THEFT ALERT` — Unusual movement pattern (ML) | The Isolation Forest confirms abnormal driving (3 of the last 5 packets). |
| `THEFT ALERT` — Faster than owner ever moves | Speed or position jump beyond the learned envelope (immediate). |
| `LOCATION UPDATE` | The alert went out with only a stale position, and a fresh GPS fix arrives within 10 minutes. |
| `CONTACT LOST` | An armed vehicle is silent for longer than `contact_timeout_minutes` (out of range, jammed, destroyed). Sent once per outage. |

Every SMS fits in one 160-character message and links to the last known location on Google Maps.

## 10. Packet security

Firmware v3 encrypts and authenticates every packet with **AES-128-GCM** (see `gateway/lora_crypto.py` for the byte layout). The gateway:
- drops packets that fail authentication: forged, altered, corrupted, or made with a different key;
- drops **replays**. The boot epoch and sequence number are inside the authenticated header and must always increase, so a recorded packet played back later is rejected with certainty;
- drops unencrypted (v1/v2) packets when `require_encryption` is on.

Each rejection is logged as `REJECTED packet: …` and counted on the dashboard's System page. Missed sequence numbers feed the Packet Delivery Ratio on the Analytics page.

## 11. Collecting evaluation results (`eval_session.py`)

Run the gateway as normal. In a **second** SSH window, label each test as you do it:

```bash
cd ~/VehicleTheftDetection/02_Edge_Gateway_RPi5/gateway
source ../venv/bin/activate

python3 eval_session.py start theft --note "armed, door opened, pushed 5 m"
python3 eval_session.py sms          # type this the moment the SMS arrives on your phone
python3 eval_session.py stop
```

| Test | How to do it | Mode |
|---|---|---|
| `start theft` | Move/drive the node as a thief would. Do ~10 armed and ~10 disarmed (the disarmed ones need unusual driving: much faster, odd direction or time). | both |
| `start normal` | Ordinary use: carry or drive the node normally for 10–30 min. Any theft alert counts as a false alarm. | **disarmed** |
| `start range --distance 50` | Leave the node at a fixed distance for ~3 min; repeat at 25 / 50 / 100 / 200 m… | either |
| `start shield` | Put the node in a metal tin (or out of range) until CONTACT LOST arrives, then stop and take it out. | **armed** |

Then build the results:

```bash
python3 eval_session.py list       # check your sessions
python3 eval_session.py report     # writes gateway/results/
```

`results/evaluation_report.md` gives detection rate (armed / disarmed), false-alarm rate, latency tables (packet → alert, alert → SMS sent, motion → SMS on your phone), PDR / RSSI / SNR per distance, and loss-of-contact detection time, plus CSV files for each test type. For charts (`latency.png`, `range.png`), first run `pip install matplotlib`. Copy the `results` folder to your PC for the report.
