#!/usr/bin/env python3
"""
============================================================================
 Edge-Based Vehicle Theft Detection System - HARDWARE-FREE SIMULATION
============================================================================
Lets anyone test the beta on an ordinary PC (Windows, macOS or Linux) with no
ESP32 node, LoRa radio, Raspberry Pi or SIM800C.

What is real and what is simulated:
    REAL       the gateway's own code (pi_gateway.py): packet parsing,
               sequence checks, both Isolation Forest models, the learning
               period, alert cooldown, follow-up location SMS, the
               loss-of-contact watchdog, the SQLite store, and the full
               multi-page dashboard (dashboard/server.py).
    SIMULATED  the vehicle node (packets are generated with the exact 31-byte
               binary format the ESP32 firmware sends), the LoRa radio, and
               the SIM800C modem (SMS messages are printed to this window
               instead of being sent).

What happens when you run it:
    1. A week of normal history is generated for vehicle 1001 (parked at the
       CUT stadium, Bloemfontein, plus two daily trips) and the Isolation
       Forest models are trained on it.
    2. The dashboard opens at http://127.0.0.1:8000
    3. Four scenarios play out live (about 3 minutes):
         A. Parked      - heartbeats judged normal, no alert
         B. Theft       - the vehicle is moved and driven away fast at an
                          hour it is never normally used: THEFT ALERT SMS,
                          then a LOCATION UPDATE SMS, live route on the map
         C. Out of range - packets stop: CONTACT LOST SMS
         D. Back in range - contact restored
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
import telemetry_store  # noqa: E402
import pi_gateway as pg  # noqa: E402

NODE_ID = 1001
SIM_PHONE = "+27100000001"          # fictitious number; nothing is sent
HOME = (-29.124075, 26.215330)       # CUT stadium area, Bloemfontein (from the field test)
WORK = (-29.098500, 26.182000)       # a destination ~4.5 km away for the daily trips
COOLDOWN_S = 30
CONTACT_TIMEOUT_S = 20


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
# Packet generation - byte-for-byte the ESP32 firmware's TelemetryPacket v2
# ---------------------------------------------------------------------------
class SimulatedNode:
    def __init__(self, node_id):
        self.node_id = node_id
        self.sequence = 0

    def packet(self, ptype, lat, lon, speed, heading, motion, gps_valid, sats=8):
        self.sequence += 1
        return struct.pack(pg.TELEMETRY_V2_FORMAT, pg.PROTOCOL_VERSION, ptype, self.node_id,
                           self.sequence, lat, lon, speed, heading, sats,
                           1 if motion else 0, 1 if gps_valid else 0, 0)


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
    """Seven days of a normally-used vehicle: parked with a heartbeat every
    5 minutes, plus two 20-minute trips a day. Trips are placed 8 and 16
    hours away from the current hour, so the moment the simulation runs is
    always outside the vehicle's usual driving times - exactly the situation
    a real night-time theft creates."""
    now = datetime.now()
    trip_hours = [(now.hour + 8) % 24, (now.hour + 16) % 24]
    rows = []
    t = now - timedelta(days=7)
    end = now - timedelta(seconds=20)
    at_home = True

    def add(ts, lat, lon, speed, heading, motion, ptype):
        node.sequence += 1
        rows.append({
            "node_id": node.node_id, "latitude": lat, "longitude": lon, "speed_kmph": speed,
            "heading_deg": heading, "satellites": 8, "motion_flag": int(motion), "gps_valid": 1,
            "battery_millivolts": 0, "sequence_number": node.sequence, "packet_type": ptype,
            "rssi_dbm": random.randint(-70, -40), "snr_db": round(random.uniform(8, 13), 1),
            "anomaly_score": None, "is_anomaly": 0, "received_at": ts.timestamp(),
        })

    while t < end:
        if t.hour in trip_hours and t.minute < 5:
            src, dst = (HOME, WORK) if at_home else (WORK, HOME)
            hdg = bearing_deg(src, dst)
            steps = 40                                   # a packet every 30 s for 20 min
            for k in range(steps):
                f = k / (steps - 1)
                lat = src[0] + (dst[0] - src[0]) * f
                lon = src[1] + (dst[1] - src[1]) * f
                add(t, lat, lon, random.uniform(30, 60), hdg, True,
                    pg.PACKET_TYPE_MOTION_UPDATE)
                t += timedelta(seconds=30)
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
               packet_type, rssi_dbm, snr_db, anomaly_score, is_anomaly, received_at)
               VALUES (:node_id, :latitude, :longitude, :speed_kmph, :heading_deg, :satellites,
               :motion_flag, :gps_valid, :battery_millivolts, :sequence_number, :packet_type,
               :rssi_dbm, :snr_db, :anomaly_score, :is_anomaly, :received_at)""", rows)
    return len(rows), trip_hours, (HOME if at_home else WORK)


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
        end = time.time() + seconds
        while time.time() < end:
            self.tick()
            time.sleep(0.5)

    def run(self):
        n = self.node

        banner("SCENARIO A - Vehicle parked (expected: judged normal, no alert)")
        note("The node sends routine heartbeats: no motion, fresh GPS fix at the parking spot.")
        for _ in range(4):
            lat, lon = jitter(self.park, 0.2)
            self.send(n.packet(pg.PACKET_TYPE_HEARTBEAT, lat, lon, 0.0, 0.0, False, True))
            self.wait(4)

        banner("SCENARIO B - Theft (expected: THEFT ALERT SMS, then LOCATION UPDATE SMS)")
        note("Someone moves the vehicle at an hour it is never normally used.")
        note("1) The motion interrupt wakes the node; it reports immediately, before the GPS locks.")
        lat, lon = jitter(self.park, 0.2)
        self.send(n.packet(pg.PACKET_TYPE_MOTION_ALERT, lat, lon, 0.0, 0.0, True, False))
        self.wait(3)
        note("2) The vehicle is driven away fast. Watch the route grow on the Live Map.")
        start = self.park
        dest = (start[0] + 0.030, start[1] + 0.035)      # ~4.7 km north-east
        hdg = bearing_deg(start, dest)
        steps = 40                                         # a packet every 2 s, ~100 km/h
        for k in range(1, steps + 1):
            f = k / steps * 0.25                           # covers ~1.2 km in 80 s
            lat = start[0] + (dest[0] - start[0]) * f
            lon = start[1] + (dest[1] - start[1]) * f
            self.send(n.packet(pg.PACKET_TYPE_MOTION_UPDATE, lat, lon,
                               random.uniform(90, 110), hdg, True, True))
            self.wait(2)
        last = (lat, lon)

        banner("SCENARIO C - Out of range / jammed (expected: CONTACT LOST SMS)")
        note(f"Packets stop. The gateway waits {CONTACT_TIMEOUT_S} s (16 min in normal use).")
        self.wait(CONTACT_TIMEOUT_S + 6)

        banner("SCENARIO D - Back in range (expected: contact restored)")
        lat, lon = jitter(last)
        self.send(n.packet(pg.PACKET_TYPE_HEARTBEAT, lat, lon, 0.0, hdg, False, True))
        note("The vehicle reports again. It stays red ('theft alert') on the dashboard")
        note("for 30 minutes after the theft alert, so a stolen car never quietly turns green.")


def main():
    ap = argparse.ArgumentParser(description="Hardware-free simulation of the vehicle theft detection beta")
    ap.add_argument("--port", type=int, default=8000, help="dashboard port (default 8000)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = ap.parse_args()
    random.seed(7)

    banner("Edge-Based Vehicle Theft Detection - hardware-free simulation")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(SIM_DB + suffix)
        except FileNotFoundError:
            pass
    os.makedirs(os.path.dirname(SIM_DB), exist_ok=True)

    # Demo timings and simulated hardware, injected into the real gateway code.
    pg.ALERT_COOLDOWN_SECONDS = COOLDOWN_S
    pg.Sim800Gsm = SimulatedModem

    node = SimulatedNode(NODE_ID)
    count, trip_hours, park = build_history(node)
    note(f"Generated {count} packets of normal history (7 days: parked + trips at "
         f"{trip_hours[0]:02d}:00 and {trip_hours[1]:02d}:00).")

    config = pg.GatewayConfig(alert_phone_numbers=[SIM_PHONE], gsm_serial_port=None,
                              contact_timeout_seconds=CONTACT_TIMEOUT_S,
                              loss_of_contact_alerts=True, known_node_ids=[NODE_ID])
    gw = pg.Gateway(config)
    radio = SimulatedRadio()
    gw._radio = radio
    # The history ends just now, so the vehicle counts as in contact (a large
    # timeout during the restore avoids a misleading 'not reported' warning).
    gw._config.contact_timeout_seconds = 10 ** 9
    gw._restore_history()           # the Isolation Forest models train from this history
    gw._config.contact_timeout_seconds = CONTACT_TIMEOUT_S
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
