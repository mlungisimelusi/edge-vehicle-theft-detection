#!/usr/bin/env python3
"""
============================================================================
 Isolation Forest benchmark - reproducible evaluation of the gateway's ML
============================================================================
Uses the gateway's REAL detector (pi_gateway.NodeAnomalyDetector, the same
features, contamination, window and 3-of-5 confirmation rule) on synthetic
trips, so the detection results in the paper can be reproduced on any PC.

Three detector variants are compared on identical data:
    beta    contamination 0.05, Isolation Forest + 3-of-5 rule (beta version)
    tuned   contamination 0.01, Isolation Forest + 3-of-5 rule
    final   tuned + learned speed envelope with immediate alert (final version)

Two profiles:
    vehicle   owner drives at 0-70 km/h in the morning and afternoon
    walking   the field-test emulation used in this project (no car):
              owner walks at 3-6 km/h; "unusual" movement is running/cycling

For each profile the model is trained on 14 days of the owner's trips
(2 per day, recorded while DISARMED), then tested on 50 held-out normal
trips and 50 trips of each anomaly type:
    speed      getaway at 110-160 km/h  (walking: 15-25 km/h)
    night      normal speeds, but at 01:00-04:00
    jump       GPS position jumps ~3 km between two packets (spoofing/towing)
    erratic    rapid heading changes with high speed bursts

Reported per trip: was an alert raised (detection rate / false alarm rate)
and how long it took (packets x packet interval). Reported per packet:
precision, recall and F1 of the single-packet Isolation Forest decision.
Also: mean inference time per packet on this computer.

Run (from this gateway/ folder):
    python ml_benchmark.py                 # both profiles, prints tables
    python ml_benchmark.py --csv results   # also writes CSV files
============================================================================
"""

import argparse
import csv
import math
import os
import random
import statistics
import sys
import time
import types
from collections import deque
from datetime import datetime

import numpy as np

# Stand-ins for the Raspberry Pi hardware libraries so pi_gateway imports on
# any PC (the radio and modem are never used by this benchmark).
if "spidev" not in sys.modules:
    _spidev = types.ModuleType("spidev")
    _spidev.SpiDev = type("SpiDev", (), {})
    sys.modules["spidev"] = _spidev
if "gpiozero" not in sys.modules:
    _gz = types.ModuleType("gpiozero")
    _gz.DigitalOutputDevice = type("DigitalOutputDevice", (), {})
    _gz.DigitalInputDevice = type("DigitalInputDevice", (), {})
    sys.modules["gpiozero"] = _gz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pi_gateway as gw  # noqa: E402

PACKET_INTERVAL_S = 10.0     # one GPS-fixed motion packet every ~10 s while moving
TRIP_PACKETS = 90            # ~15-minute trip
TRAIN_DAYS = 14
TEST_TRIPS = 50
BASE_LAT, BASE_LON = -29.1100, 26.2140   # CUT, Bloemfontein

PROFILES = {
    "vehicle": {"min": 5.0, "cruise": 45.0, "sd": 6.0, "stop": 0.05,
                "fast": (110.0, 160.0), "burst": (60.0, 120.0)},
    "walking": {"min": 3.2, "cruise": 4.8, "sd": 0.4, "stop": 0.05,
                "fast": (15.0, 25.0), "burst": (8.0, 20.0)},
}

# Detector variants compared (all use the gateway's own features and 3-of-5 rule)
VARIANTS = {
    "beta":  {"contamination": 0.05, "envelope": False},   # beta version: Isolation Forest only
    "tuned": {"contamination": 0.01, "envelope": False},   # lower contamination only
    "final": {"contamination": gw.ISOLATION_FOREST_CONTAMINATION, "envelope": True},  # as in pi_gateway.py
}
ENVELOPE_SPEED_FACTOR = gw.ENVELOPE_SPEED_FACTOR       # > 1.25 x the owner's fastest recorded speed
ENVELOPE_IMPLIED_FACTOR = gw.ENVELOPE_IMPLIED_FACTOR   # position jump faster than 1.5 x that


def _epoch(day: int, hour: float) -> float:
    base = datetime(2026, 9, 1).timestamp()
    return base + day * 86400 + hour * 3600


def make_trip(profile: dict, rng: random.Random, start: float, kind: str = "normal"):
    """Returns a list of (Telemetry, is_anomalous_packet). Normal speed is a
    mean-reverting random process around the owner's cruising speed with
    occasional slow-downs (intersections, crowds)."""
    lat, lon = BASE_LAT + rng.uniform(-0.01, 0.01), BASE_LON + rng.uniform(-0.01, 0.01)
    heading = rng.uniform(0, 360)
    speed = profile["cruise"]
    jump_at = rng.randint(20, TRIP_PACKETS - 20)
    out = []
    for i in range(TRIP_PACKETS):
        ts = start + i * PACKET_INTERVAL_S
        anomalous = kind in ("speed", "night", "erratic")
        if kind == "speed":
            speed = rng.uniform(*profile["fast"])
        elif kind == "erratic":
            speed = rng.uniform(*profile["burst"])
            heading = (heading + rng.choice([-1, 1]) * rng.uniform(60, 150)) % 360
        else:
            speed += 0.3 * (profile["cruise"] - speed) + rng.gauss(0, profile["sd"])
            if rng.random() < profile["stop"]:
                speed = profile["min"]
            speed = max(profile["min"], speed)
            if rng.random() < 0.05:           # ordinary turns at intersections
                heading = (heading + rng.choice([-90, 90])) % 360
            heading = (heading + rng.gauss(0, 4)) % 360
        km = speed * PACKET_INTERVAL_S / 3600.0
        lat += km / 111.0 * math.cos(math.radians(heading))
        lon += km / (111.0 * math.cos(math.radians(lat))) * math.sin(math.radians(heading))
        if kind == "jump":
            anomalous = i == jump_at
            if anomalous:
                lat += 0.027                  # ~3 km in one packet interval
        t = gw.Telemetry(node_id=1001, latitude=lat, longitude=lon, speed_kmph=speed,
                         heading_deg=heading, satellites=8, motion_flag=True, gps_valid=True,
                         battery_millivolts=0, rssi_dbm=-90, snr_db=7.0, received_at=ts)
        out.append((t, anomalous))
    return out


def training_trips(profile: dict, rng: random.Random):
    trips = []
    for day in range(TRAIN_DAYS):
        for hour in (rng.uniform(6.5, 8.5), rng.uniform(16.0, 18.5)):
            trips.append(make_trip(profile, rng, _epoch(day, hour)))
    return trips


def train_detector(trips, contamination: float) -> gw.NodeAnomalyDetector:
    old = gw.ISOLATION_FOREST_CONTAMINATION
    gw.ISOLATION_FOREST_CONTAMINATION = contamination
    try:
        det = gw.NodeAnomalyDetector(1001)
        for trip in trips:
            prev = None
            for t, _ in trip:
                det.learn(t, prev)
                prev = t
        det.movement_model.fit_if_due()
    finally:
        gw.ISOLATION_FOREST_CONTAMINATION = old
    return det


def run_trip(det: gw.NodeAnomalyDetector, trip, envelope=None, time_packets: int = 0):
    """Mirror of Gateway._process_packet's ML decision, without learning.
    The model does not change while testing (learn=False), so the whole trip
    is scored in one batch - identical decisions to per-packet
    det.evaluate(), much faster. The first `time_packets` packets are also
    run through det.evaluate() one at a time to measure real inference time."""
    timings = []
    prev = None
    for t, _ in trip[:time_packets]:
        t0 = time.perf_counter()
        det.evaluate(t, prev, learn=False)
        timings.append(time.perf_counter() - t0)
        prev = t
    feats, prev = [], None
    for t, _ in trip:
        feats.append(gw.movement_features(t, prev))
        prev = t
    feats = np.vstack(feats)
    flags = det.movement_model.model.predict(feats) == -1
    hard = np.zeros(len(flags), dtype=bool)
    if envelope is not None:
        # Physically implausible for this owner: alert at once, no 3-of-5
        # wait (a GPS jump is a single packet and could never collect 3 votes).
        max_speed, max_implied = envelope
        hard = (feats[:, 0] > max_speed) | (feats[:, 3] > max_implied)
        flags |= hard

    votes = deque(maxlen=gw.ML_CONFIRM_WINDOW)
    single_alert_at = confirmed_at = None
    packet_results = []
    confirmed = []
    for i, ((t, truth), is_out) in enumerate(zip(trip, flags)):
        votes.append(bool(is_out))
        packet_results.append((bool(is_out), truth))
        ok = bool(is_out) and (bool(hard[i]) or sum(votes) >= gw.ML_CONFIRM_COUNT)
        confirmed.append(ok)
        if is_out and single_alert_at is None:
            single_alert_at = i
        if ok and confirmed_at is None:
            confirmed_at = i
    return single_alert_at, confirmed_at, packet_results, timings, confirmed


def _prf(packets):
    tp = sum(1 for p, t in packets if p and t)
    fp = sum(1 for p, t in packets if p and not t)
    fn = sum(1 for p, t in packets if not p and t)
    tn = sum(1 for p, t in packets if not p and not t)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": round(precision, 3),
            "recall": round(recall, 3), "f1": round(f1, 3),
            "accuracy": round((tp + tn) / max(1, len(packets)), 3)}


def evaluate_profile(name: str, seed: int):
    """Same training data and the same test trips for every variant."""
    profile = PROFILES[name]
    rng = random.Random(seed)
    train = training_trips(profile, rng)
    tests = {}
    for kind in ("normal", "speed", "night", "jump", "erratic"):
        tests[kind] = []
        for k in range(TEST_TRIPS):
            hour = rng.uniform(1.0, 4.0) if kind == "night" else rng.choice(
                [rng.uniform(6.5, 8.5), rng.uniform(16.0, 18.5)])
            tests[kind].append(make_trip(profile, rng, _epoch(TRAIN_DAYS + k, hour), kind))

    rows, metrics = [], []
    for vname, v in VARIANTS.items():
        det = train_detector(train, v["contamination"])
        envelope = None
        if v["envelope"]:
            hist = np.vstack(det.movement_model.history)
            envelope = (ENVELOPE_SPEED_FACTOR * hist[:, 0].max(),
                        ENVELOPE_IMPLIED_FACTOR * ENVELOPE_SPEED_FACTOR * hist[:, 0].max())
        all_packets, all_timings = [], []
        for kind, trips in tests.items():
            single_hits = confirm_hits = 0
            delays = []
            for k, trip in enumerate(trips):
                s_at, c_at, pk, tm, conf = run_trip(det, trip, envelope, time_packets=5 if k < 4 else 0)
                all_packets += pk
                all_timings += tm
                if kind == "normal":
                    single_hits += s_at is not None
                    confirm_hits += c_at is not None
                    continue
                first_bad = next(i for i, (_, truth) in enumerate(trip) if truth)
                # An alert only counts as a detection at or after the first
                # anomalous packet (earlier ones are false alarms).
                window_end = TRIP_PACKETS if kind != "jump" else first_bad + gw.ML_CONFIRM_WINDOW
                s_ok = any(p for p, _ in pk[first_bad:window_end])
                c_idx = next((i for i in range(first_bad, window_end) if conf[i]), None)
                single_hits += s_ok
                if c_idx is not None:
                    confirm_hits += 1
                    delays.append((c_idx - first_bad + 1) * PACKET_INTERVAL_S)
            rows.append({
                "profile": name, "variant": vname, "trip_type": kind, "trips": len(trips),
                "alert_rate_single_packet_pct": round(100.0 * single_hits / len(trips), 1),
                "alert_rate_3of5_pct": round(100.0 * confirm_hits / len(trips), 1),
                "median_time_to_alert_s": round(statistics.median(delays), 1) if delays else None,
            })
        m = {"profile": name, "variant": vname, "packets": len(all_packets)}
        m.update(_prf(all_packets))
        m["mean_inference_ms"] = round(1000 * statistics.mean(all_timings), 2)
        m["training_samples"] = len(det.movement_model.history)
        metrics.append(m)
    return rows, metrics


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", metavar="DIR", help="write trip and packet results as CSV here")
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()

    all_rows, all_metrics = [], []
    for name in ("vehicle", "walking"):
        rows, metrics = evaluate_profile(name, args.seed)
        all_rows += rows
        all_metrics += metrics
        for m in metrics:
            v = m["variant"]
            print(f"\n=== {name} / {v}  (contamination {VARIANTS[v]['contamination']}, "
                  f"envelope {'on' if VARIANTS[v]['envelope'] else 'off'}, "
                  f"{m['training_samples']} training packets) ===")
            print(f"{'trip type':<10}{'single-packet alert':>22}{'3-of-5 alert':>15}{'median time':>14}")
            for r in rows:
                if r["variant"] != v:
                    continue
                mt = f"{r['median_time_to_alert_s']} s" if r["median_time_to_alert_s"] is not None else "-"
                print(f"{r['trip_type']:<10}{r['alert_rate_single_packet_pct']:>21}%"
                      f"{r['alert_rate_3of5_pct']:>14}%{mt:>14}")
            print(f"packet level: precision {m['precision']}, recall {m['recall']}, F1 {m['f1']}, "
                  f"accuracy {m['accuracy']} (TP {m['tp']}, FP {m['fp']}, FN {m['fn']}, TN {m['tn']})")
            print(f"mean inference time: {m['mean_inference_ms']} ms per packet")
        print("(normal row = FALSE-ALARM rate per trip; other rows = DETECTION rate)")

    if args.csv:
        os.makedirs(args.csv, exist_ok=True)
        for fname, data in (("ml_benchmark_trips.csv", all_rows), ("ml_benchmark_packets.csv", all_metrics)):
            with open(os.path.join(args.csv, fname), "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(data[0].keys()))
                w.writeheader()
                w.writerows(data)
        print(f"\nCSV written to {args.csv}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
