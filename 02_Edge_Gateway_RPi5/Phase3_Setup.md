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
  "known_node_ids": [1001]
}
```

- `alert_phone_numbers` — the number(s) that receive SMS alerts, in international format (`+27…`). Local format (`082…`) and placeholder numbers are rejected at startup with a clear log message. You can also pass them as an environment variable instead: `VTD_ALERT_NUMBERS="+27821234567,+27831234567"`.
- `gsm_serial_port` — the SIM800C's UART. `/dev/ttyAMA0` is what this Pi 5 uses (confirmed with minicom). Set it to `null` to auto-detect.
- `contact_timeout_minutes` — how long a vehicle may be silent before a **loss-of-contact** SMS is sent. The node sends a heartbeat every 5 minutes, so 16 minutes = 3 missed heartbeats plus a margin.
- `known_node_ids` — only packets from these node IDs are processed; anything else on 433 MHz is ignored. Leave the list empty to accept any node.

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
Node 1001: restored 1432 samples of history (movement model: 610), last seen 22:25.
SX127x detected, REG_VERSION=0x12
SX127x configured: freq=433.000MHz SF=9 BW=125000Hz CR=4/5 sync=0x12 preamble=8
LoRa receiver armed — listening continuously on DIO0 (GPIO24) interrupt.
SIM800C responding at 9600 baud on /dev/ttyAMA0.
SIM800C ready on /dev/ttyAMA0, network registered, signal ~-65 dBm.
Gateway running — 1 alert number(s), loss-of-contact alerts ON (16 min). Listening for telemetry...
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

## 7. Isolation Forest behaviour, in plain terms

Each vehicle gets its own models trained on its own history (a rolling window of the last 2000 packets, about a week of 5-minute heartbeats) — a theft pattern on one vehicle shouldn't be judged against another vehicle's habits.

There are two models per vehicle, because not every packet carries the same information:

- **Movement model** — for packets with a fresh GPS fix: speed, heading, the speed implied by the distance between consecutive fixes (catches position jumps such as towing or GPS spoofing), whether the motion sensor fired, and time of day.
- **Context model** — for packets without a fresh fix (for example the immediate motion packet the node sends before its GPS has locked): motion flag and time of day only. A stale-GPS packet can't be judged on speed and heading it doesn't have, and its zeros are never fed into the movement model, where they would teach it a false "normal".

Heading and time of day are encoded as sine/cosine pairs, so 23:00 is next to 00:00 and 359° is next to 1°.

Each model has a **learning period**: the context model needs `CONTEXT_MIN_SAMPLES` (288 packets, about a day of 5-minute heartbeats) and the movement model `MOVEMENT_MIN_SAMPLES` (100 fresh GPS fixes). During it, the gateway uses a simple rule: if the node's hardware motion interrupt fired, treat it as an anomaly rather than staying silent while it learns. The period was originally 20 samples, and that was too short: after a few bench shake tests the model had learned "being shaken at this hour" as normal and stopped alerting. The startup log shows each vehicle's learning progress, and every packet that doesn't raise an alert is logged with the reason (still learning, or judged normal with its score). Once trained, each model refits every 5 new samples and scores each new packet against the baseline fitted *before* that packet was added, so a packet never helps judge itself.

Model history is rebuilt from the database on startup, so restarting the gateway doesn't send every vehicle back to the cold-start rule.

A 5-minute cooldown (`ALERT_COOLDOWN_SECONDS`) per vehicle prevents SMS spam if a vehicle keeps triggering anomalies (e.g. genuinely being driven away) — every anomaly is still recorded, just not re-texted every few seconds.

With `contamination = 0.05`, roughly 5% of normal packets will still be scored as outliers. That's inherent to the algorithm, not a bug; the cooldown limits how often it becomes an SMS.

## 7a. The three alert types

| SMS | When | Why it exists |
|---|---|---|
| `THEFT ALERT` | A packet is scored anomalous (or the cold-start motion rule fires). | The core detection path. |
| `LOCATION UPDATE` | The alert above went out with only a stale position, and a fresh GPS fix arrives within 10 minutes. | The node now reports motion *before* waiting for GPS, so the first SMS is fast but may only have the last known position. |
| `CONTACT LOST` | A vehicle that was reporting is silent for longer than `contact_timeout_minutes`. | A car driven out of LoRa range, a jammed 433 MHz channel, or a destroyed node never sends an anomalous packet — silence itself is the signal. Sent once per outage; the next packet clears it. |

Every SMS is kept under 160 characters (the SIM800C's single-SMS limit in text mode) and links to the last known location on Google Maps.

Note: with the gateway at home, `CONTACT LOST` will also fire when the owner legitimately drives out of LoRa range. That's an inherent limit of a single fixed gateway; set `loss_of_contact_alerts` to `false` if it's unwanted during testing.

## 8. Packet sequence numbers and the remaining encryption gap

Node firmware v2 sends a sequence number with every packet. The gateway uses it to drop duplicate packets, drop packets whose sequence number jumps backwards (a naive replay of a recorded packet) unless the node is clearly restarting its count, and count missed packets — which the dashboard's Analytics page turns into a **Packet Delivery Ratio**. Legacy 29-byte v1 packets are still accepted, with a warning to reflash the node.

Sequence numbers are not cryptographic: someone who knows the packet format could still forge a packet with a higher number. The telemetry is still sent unencrypted and unauthenticated. The proposal's "LoRa packet decryption" needs a shared symmetric key on both ends (AES-128 via `mbedtls` on the ESP32 and `pycryptodome` on the Pi), ideally with an authentication tag (AES-GCM or HMAC) so forged packets are rejected. This remains future work.
