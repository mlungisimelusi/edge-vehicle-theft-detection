#!/usr/bin/env python3
"""
============================================================================
 Edge-Based Vehicle Theft Detection System - HARDWARE-FREE SIMULATION
============================================================================
Lets anyone test the beta on an ordinary PC (Windows, macOS or Linux) with no
ESP32 node, LoRa radio, Raspberry Pi or SIM800C.

What is real and what is simulated:
    REAL       the gateway's own code (pi_gateway.py): AES-128-GCM decryption
               and authentication, replay protection, arm/disarm logic, both
               Isolation Forest models, alert cooldown, follow-up location
               SMS, the loss-of-contact watchdog, the SQLite store, and the
               full multi-page dashboard (dashboard/server.py), including the
               PIN-protected arm/disarm button and the two-colour route.
    SIMULATED  the vehicle node (packets are encrypted into the exact 51-byte
               v3 format the ESP32 firmware sends, with a random demo key),
               the LoRa radio, and the SIM800C modem (SMS messages are printed
               to this window instead of being sent).

What happens when you run it:
    1. A week of normal history is generated for vehicle 1001 (parked at the
       CUT stadium, Bloemfontein, armed; disarmed for two short trips a day)
       and the Isolation Forest models are trained on it.
    2. The dashboard opens at http://127.0.0.1:8000 (arm/disarm PIN: 1234)
    3. Seven scenarios play out live (about 4 minutes):
         A. Parked, ARMED            - heartbeats judged normal, no alert
         B. Owner DISARMS and drives - judged normal by the Isolation Forest,
                                       no alert; route drawn BLUE; re-armed
         C. Theft while ARMED        - "Moved while ARMED" SMS, then a
                                       LOCATION UPDATE SMS; route turns RED
         D. Out of range             - packets stop: CONTACT LOST SMS
         E. Back in range            - contact restored
         F. Attacks                  - a replayed packet, a forged packet and
                                       an unencrypted packet are all REJECTED
         G. Unusual driving while DISARMED - the Isolation Forest alone raises
                                       "Faster than owner ever moves" (learned
                                       speed envelope checked with the ML)
    4. The dashboard stays open so the results can be explored
       (Live Map, Fleet, Vehicle detail, Alerts, Analytics, System).

Timing is compressed for the demo: heartbeats every few seconds instead of
every 5 minutes, a 30 s SMS cooldown instead of 5 minutes, and a 20 s
loss-of-contact timeout instead of 16 minutes.

Run (from this gateway/ folder, after installing requirements-simulation.txt):
    python simulate.py
Stop:
    Ctrl+C
Options:
    --no-browser      don't open the browser automatically
    --port 8000       dashboard port
    --quick           halve all waits (used by the automated tests)
============================================================================
"""

import argparse
import math
import os
import random
import sqlite3
import struct
import sys
import threading
import time
import types
import webbrowser
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SIM_DB = os.path.join(HERE, "data", "simulation.db")

# --- 1. Separate database: the simulation never touches real gateway data.
os.environ["VTD_DB_PATH"] = SIM_DB
# Dashboard PIN for the arm/disarm button during the simulation.
SIM_PIN = "1234"
os.environ["VTD_DASHBOARD_PIN"] = SIM_PIN

# --- 2. Stand-ins for the Raspberry Pi hardware libraries, so the real
#        gateway modules import on any PC. They are never actually used:
#        the radio and modem are replaced below.
if "spidev" not in sys.modules:
    spidev = types.ModuleType("spidev")
    spidev.SpiDev = type("SpiDev", (), {})
    sys.modules["spidev"] = spidev
if "gpiozero" not in sys.modules:
    gpiozero = types.ModuleType("gpiozero")
    gpiozero.DigitalOutputDevice = type("DigitalOutputDevice", (), {})
    gpiozero.DigitalInputDevice = type("DigitalInputDevice", (), {})
    sys.modules["gpiozero"] = gpiozero

sys.path.insert(0, HERE)
import lora_crypto  # noqa: E402
import telemetry_store  # noqa: E402
import pi_gateway as pg  # noqa: E402

NODE_ID = 1001
SIM_PHONE = "+27100000001"          # fictitious number; nothing is sent
HOME = (-29.124075, 26.215330)       # CUT stadium area, Bloemfontein (from the field test)
WORK = (-29.098500, 26.182000)       # a destination ~4.5 km away for the daily trips
COOLDOWN_S = 30
CONTACT_TIMEOUT_S = 20
SIM_KEY = os.urandom(16)            # demo AES-128 key, new on every run
SPEED = 1.0                          # time scale; --quick sets 0.5


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------
def banner(text):
    line = "=" * 74
    print(f"\n{line}\n  {text}\n{line}", flush=True)


def note(text):
    print(f"  >> {text}", flush=True)


# ---------------------------------------------------------------------------
# Simulated hardware
# ---------------------------------------------------------------------------
class SimulatedModem:
    """Drop-in replacement for Sim800Gsm: prints each SMS instead of sending it."""

    def __init__(self, port=None):
        self.port_name = "simulated modem"

    def open(self):
        return True

    def close(self):
        pass

    def reopen(self):
        return True

    def sim_ready(self):
        return True

    def is_network_registered(self, timeout_s=0):
        return True

    def signal_quality(self):
        return -67

    def is_alive(self):
        return True

    def send_sms(self, number, message):
        width = 52
        print("\n  +" + "-" * width + "+", flush=True)
        print("  | " + f"SMS to {number} (simulated)".ljust(width - 1) + "|")
        print("  +" + "-" * width + "+")
        for line in message.splitlines():
            while line:
                print("  | " + line[: width - 2].ljust(width - 1) + "|")
                line = line[width - 2:]
        print("  +" + "-" * width + "+\n", flush=True)
        return True


class SimulatedRadio:
    """Stands in for the SX1278 receiver: only counts packets for the System page."""

    def __init__(self):
        self.packets_received = 0
        self.crc_errors = 0

    def ensure_receiving(self):
        return True

    def stop_receiving(self):
        pass

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Packet generation - byte-for-byte the ESP32 firmware's encrypted packet v3
# ---------------------------------------------------------------------------
class SimulatedNode:
    def __init__(self, node_id):
        self.node_id = node_id
        self.boot_epoch = 1
        self.sequence = 0

    def packet(self, ptype, lat, lon, speed, heading, motion, gps_valid, sats=8):
        self.sequence += 1
        return lora_crypto.encrypt_packet(SIM_KEY, ptype, self.node_id, self.boot_epoch, self.sequence,
                                          lat, lon, speed, heading, sats, motion, gps_valid, 0)


def bearing_deg(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def jitter(pos, metres=3.0):
    """Typical stationary GPS wander."""
    d = metres / 111_320.0
    return (pos[0] + random.uniform(-d, d), pos[1] + random.uniform(-d, d))


# ---------------------------------------------------------------------------
# Step 1: a week of normal history
# ---------------------------------------------------------------------------
def build_history(node):
    """Seven days of a normally-used vehicle: parked and ARMED with a
    heartbeat every 5 minutes, and DISARMED by the owner for two ~6-minute
    trips a day (HOME -> WORK and back). The first daily trip starts at the
    time of day the simulation's 'owner drives' scenario (B) runs, so that
    drive matches the owner's habits; the second is 9 hours later. Arm and
    disarm events are recorded exactly as the dashboard button would, so the
    gateway learns only from disarmed driving and parked time."""
    now = datetime.now()
    first_trip_offset = timedelta(minutes=-2)            # around when scenario B runs
    trip_starts = []
    for day in range(7, 0, -1):
        base = now - timedelta(days=day) + first_trip_offset
        trip_starts += [base, base + timedelta(hours=9)]
    rows, mode_events = [], []
    t = now - timedelta(days=7)
    end = now - timedelta(seconds=20)
    at_home = True
    next_trip = 0

    def add(ts, lat, lon, speed, heading, motion, ptype):
        node.sequence += 1
        rows.append({
            "node_id": node.node_id, "latitude": lat, "longitude": lon, "speed_kmph": speed,
            "heading_deg": heading, "satellites": 8, "motion_flag": int(motion), "gps_valid": 1,
            "battery_millivolts": 0, "sequence_number": node.sequence, "packet_type": ptype,
            "rssi_dbm": random.randint(-70, -40), "snr_db": round(random.uniform(8, 13), 1),
            "anomaly_score": None, "is_anomaly": 0, "received_at": ts.timestamp(),
            "boot_epoch": node.boot_epoch, "protocol_version": lora_crypto.PROTOCOL_VERSION_ENCRYPTED,
        })

    while t < end:
        if next_trip < len(trip_starts) and t >= trip_starts[next_trip]:
            next_trip += 1
            mode_events.append((t.timestamp() - 30, 0))      # owner disarms
            src, dst = (HOME, WORK) if at_home else (WORK, HOME)
            hdg = bearing_deg(src, dst)
            steps = 12                                       # a packet every 30 s, ~45 km/h
            for k in range(1, steps + 1):
                f = k / steps
                lat = src[0] + (dst[0] - src[0]) * f
                lon = src[1] + (dst[1] - src[1]) * f
                add(t, lat, lon, random.uniform(40, 50), hdg, True, pg.PACKET_TYPE_MOTION_UPDATE)
                t += timedelta(seconds=30)
            mode_events.append((t.timestamp() + 30, 1))      # owner arms again when parked
            at_home = not at_home
            continue
        here = HOME if at_home else WORK
        lat, lon = jitter(here)
        add(t, lat, lon, 0.0, 0.0, False, pg.PACKET_TYPE_HEARTBEAT)
        t += timedelta(minutes=5)

    telemetry_store.init_db()
    with sqlite3.connect(SIM_DB) as conn:
        conn.executemany(
            """INSERT INTO telemetry (node_id, latitude, longitude, speed_kmph, heading_deg,
               satellites, motion_flag, gps_valid, battery_millivolts, sequence_number,
               packet_type, rssi_dbm, snr_db, anomaly_score, is_anomaly, received_at,
               boot_epoch, protocol_version)
               VALUES (:node_id, :latitude, :longitude, :speed_kmph, :heading_deg, :satellites,
               :motion_flag, :gps_valid, :battery_millivolts, :sequence_number, :packet_type,
               :rssi_dbm, :snr_db, :anomaly_score, :is_anomaly, :received_at,
               :boot_epoch, :protocol_version)""", rows)
        conn.executemany("INSERT INTO vehicle_mode_events (node_id, armed, changed_at, changed_by) "
                         "VALUES (?, ?, ?, 'owner (simulated)')",
                         [(node.node_id, armed, ts) for ts, armed in mode_events])
    telemetry_store.set_vehicle_armed(node.node_id, True, "owner (simulated)")
    return len(rows), first_trip_offset, (HOME if at_home else WORK)


# ---------------------------------------------------------------------------
# Dashboard (the real FastAPI app) in a background thread
# ---------------------------------------------------------------------------
def start_dashboard(port):
    import uvicorn
    sys.path.insert(0, os.path.join(HERE, "..", "dashboard"))
    import server  # noqa: E402  (the real dashboard backend)
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    threading.Thread(target=srv.run, name="dashboard", daemon=True).start()
    for _ in range(50):
        if srv.started:
            break
        time.sleep(0.1)
    return srv


# ---------------------------------------------------------------------------
# Scenario playback
# ---------------------------------------------------------------------------
class Simulation:
    def __init__(self, gateway, radio, node, park):
        self.gw = gateway
        self.radio = radio
        self.node = node
        self.park = park

    def send(self, payload, rssi=None, snr=None):
        self.radio.packets_received += 1
        self.gw._process_packet(payload, rssi if rssi is not None else random.randint(-75, -45),
                                snr if snr is not None else round(random.uniform(7, 12), 1), time.time())
        self.tick()

    def tick(self):
        self.gw._check_contact()
        self.gw._write_status()

    def wait(self, seconds):
        end = time.time() + seconds * SPEED
        while time.time() < end:
            self.tick()
            time.sleep(0.5)

    def run(self):
        n = self.node

        banner("SCENARIO A - Vehicle parked and ARMED (expected: judged normal, no alert)")
        note("The node sends routine encrypted heartbeats: no motion, fresh GPS fix at the parking spot.")
        for _ in range(4):
            lat, lon = jitter(self.park, 0.2)
            self.send(n.packet(pg.PACKET_TYPE_HEARTBEAT, lat, lon, 0.0, 0.0, False, True))
            self.wait(4)

        banner("SCENARIO B - Owner DISARMS and drives as usual (expected: judged normal, no alert)")
        note(f"The owner enters the PIN and disarms (you can try it on the vehicle page: PIN {SIM_PIN}).")
        telemetry_store.set_vehicle_armed(n.node_id, False, "owner (simulated)")
        self.wait(3)
        note("The usual trip at the usual time: only the Isolation Forest could raise an alert now.")
        start = self.park
        dest = WORK if start == HOME else HOME
        hdg = bearing_deg(start, dest)
        lat, lon = jitter(start, 0.2)
        self.send(n.packet(pg.PACKET_TYPE_MOTION_ALERT, lat, lon, 0.0, 0.0, True, False))
        self.wait(2)
        frac_per_step = 0.025 / 4.6 * SPEED              # ~25 m per 2 s of real time = ~45 km/h
        for k in range(1, 21):
            f = k * frac_per_step
            lat = start[0] + (dest[0] - start[0]) * f
            lon = start[1] + (dest[1] - start[1]) * f
            self.send(n.packet(pg.PACKET_TYPE_MOTION_UPDATE, lat, lon, random.uniform(42, 48), hdg, True, True))
            self.wait(2)
        note("No alert: this matches the owner's learned behaviour. The route is drawn BLUE.")
        note("The owner parks and ARMS the vehicle again.")
        telemetry_store.set_vehicle_armed(n.node_id, True, "owner (simulated)")
        parked = (lat, lon)
        self.wait(3)

        banner("SCENARIO C - Theft while ARMED (expected: 'Moved while ARMED' SMS, then LOCATION UPDATE)")
        note("The vehicle is armed, so ANY motion is a theft alert - no model needed.")
        note("1) The motion interrupt wakes the node; it reports immediately, before the GPS locks.")
        captured = n.packet(pg.PACKET_TYPE_MOTION_ALERT, parked[0], parked[1], 0.0, 0.0, True, False)
        self.captured = captured                      # an eavesdropper records this one
        self.send(captured)
        self.wait(3)
        note("2) The vehicle is driven away fast. Watch the route turn RED on the Live Map.")
        away = (parked[0] + 0.030, parked[1] + 0.035)     # north-east
        hdg = bearing_deg(parked, away)
        frac_per_step = 0.055 / 4.6 * SPEED               # ~55 m per 2 s of real time = ~100 km/h
        for k in range(1, 31):
            f = k * frac_per_step
            lat = parked[0] + (away[0] - parked[0]) * f
            lon = parked[1] + (away[1] - parked[1]) * f
            self.send(n.packet(pg.PACKET_TYPE_MOTION_UPDATE, lat, lon,
                               random.uniform(90, 110), hdg, True, True))
            self.wait(2)
        last = (lat, lon)

        banner("SCENARIO D - Out of range / jammed (expected: CONTACT LOST SMS)")
        note(f"Packets stop. The gateway waits {CONTACT_TIMEOUT_S} s (16 min in normal use).")
        self.wait(CONTACT_TIMEOUT_S + 6)

        banner("SCENARIO E - Back in range (expected: contact restored)")
        lat, lon = jitter(last, 0.2)
        self.send(n.packet(pg.PACKET_TYPE_HEARTBEAT, lat, lon, 0.0, hdg, False, True))
        note("The vehicle reports again. It stays red ('theft alert') on the dashboard until the")
        note("owner disarms it (acknowledging the alert) or 30 minutes pass.")
        self.wait(2)

        banner("SCENARIO F - Attacks on the LoRa link (expected: all three REJECTED)")
        note("1) Replay: the attacker re-transmits the packet recorded in scenario C.")
        self.send(self.captured)
        note("2) Forgery: one byte of a genuine packet is altered (e.g. to fake a position).")
        forged = bytearray(n.packet(pg.PACKET_TYPE_HEARTBEAT, lat, lon, 0.0, 0.0, False, True))
        forged[20] ^= 0x01
        self.send(bytes(forged))
        note("3) Unencrypted: a packet in the old v2 format claims vehicle 1001 is 'parked, all fine'.")
        plain = struct.pack(pg.TELEMETRY_V2_FORMAT, 2, pg.PACKET_TYPE_HEARTBEAT, n.node_id, 999_999,
                            self.park[0], self.park[1], 0.0, 0.0, 8, 0, 1, 0)
        self.send(plain)
        note("All three were dropped - see 'REJECTED packet' above and the System page counter.")
        self.wait(3)

        banner("SCENARIO G - Unusual driving while DISARMED (expected: ML alert)")
        note("The vehicle is disarmed, then driven off at ~140 km/h in an unusual direction,")
        note("e.g. by someone with the keys. This alert comes from the Isolation Forest alone.")
        telemetry_store.set_vehicle_armed(n.node_id, False, "owner (simulated)")
        self.wait(3)
        here = (lat, lon)
        south = (here[0] - 0.05, here[1] + 0.01)
        hdg2 = bearing_deg(here, south)
        frac_per_step = 0.078 / 5.6 * SPEED               # ~78 m per 2 s of real time = ~140 km/h
        for k in range(1, 11):
            f = k * frac_per_step
            lat = here[0] + (south[0] - here[0]) * f
            lon = here[1] + (south[1] - here[1]) * f
            self.send(n.packet(pg.PACKET_TYPE_MOTION_UPDATE, lat, lon, random.uniform(135, 145), hdg2, True, True))
            self.wait(2)


def main():
    ap = argparse.ArgumentParser(description="Hardware-free simulation of the vehicle theft detection beta")
    ap.add_argument("--port", type=int, default=8000, help="dashboard port (default 8000)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    ap.add_argument("--quick", action="store_true", help="halve all waits (automated tests)")
    args = ap.parse_args()
    random.seed(7)
    global SPEED
    if args.quick:
        SPEED = 0.5

    banner("Edge-Based Vehicle Theft Detection - hardware-free simulation")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(SIM_DB + suffix)
        except FileNotFoundError:
            pass
    os.makedirs(os.path.dirname(SIM_DB), exist_ok=True)

    # Demo timings and simulated hardware, injected into the real gateway code.
    pg.ALERT_COOLDOWN_SECONDS = COOLDOWN_S * SPEED
    pg.Sim800Gsm = SimulatedModem

    node = SimulatedNode(NODE_ID)
    count, _, park = build_history(node)
    note(f"Generated {count} encrypted packets of normal history (7 days: parked and ARMED, "
         f"disarmed for two short trips a day).")

    config = pg.GatewayConfig(alert_phone_numbers=[SIM_PHONE], gsm_serial_port=None,
                              contact_timeout_seconds=CONTACT_TIMEOUT_S * SPEED,
                              loss_of_contact_alerts=True, known_node_ids=[NODE_ID],
                              lora_key=SIM_KEY, require_encryption=True, default_armed=True)
    gw = pg.Gateway(config)
    radio = SimulatedRadio()
    gw._radio = radio
    # The history ends just now, so the vehicle counts as in contact (a large
    # timeout during the restore avoids a misleading 'not reported' warning).
    gw._config.contact_timeout_seconds = 10 ** 9
    gw._restore_history()           # the Isolation Forest models train from this history
    gw._config.contact_timeout_seconds = CONTACT_TIMEOUT_S * SPEED
    for state in gw._nodes.values():
        state.contact_lost = False
        state.last_seen = time.time()
    gw._alerts.connect()
    gw._alerts.start()
    gw._write_status()

    url = f"http://127.0.0.1:{args.port}"
    srv = start_dashboard(args.port)
    if srv.started:
        note(f"Dashboard running at {url}  (keep it open beside this window)")
        note(f"Arm/disarm PIN for this simulation: {SIM_PIN}")
        if not args.no_browser:
            webbrowser.open(url)
    else:
        note(f"Could not start the dashboard on port {args.port} - is it in use? Try --port 8010.")

    note("Starting in 5 seconds...")
    time.sleep(5)
    try:
        Simulation(gw, radio, node, park).run()
        time.sleep(2)   # let the alert worker print the last SMS
        banner("Simulation complete")
        note(f"Explore the results at {url}: Live Map, Fleet > Vehicle 1001, Alerts,")
        note("Analytics (packet delivery ratio) and System (gateway/modem/radio health).")
        note("Press Ctrl+C to exit.")
        while True:
            gw._write_status()
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
    finally:
        gw._alerts.stop()
        srv.should_exit = True


if __name__ == "__main__":
    main()
