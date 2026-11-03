#!/usr/bin/env python3
"""
============================================================================
 Validation-split benchmark for the gateway's detector
============================================================================
Answers the question "were the detector settings tuned on the same data
they were evaluated on?" by separating tuning from testing:

  1. TUNING (seed 1 only): every combination of contamination
     {0.005, 0.01, 0.02, 0.05} and speed-envelope factor {1.1, 1.25, 1.5}
     is trained and tested. The configuration with the lowest false-alarm
     rate on normal trips, among those detecting >= 95 % of the speed, jump
     and erratic anomaly trips, is selected.
  2. TESTING (seeds 2-6, never used for tuning): the selected configuration
     and the baseline (contamination 0.05, no envelope) are each retrained
     on fresh owner data for every seed and tested on fresh trips. Results
     are reported as mean and standard deviation over the five seeds.

Uses the same trip generator, features and 3-of-5 rule as ml_benchmark.py.
Run (from this gateway/ folder):  python ml_validate.py [--csv results]
============================================================================
"""
import argparse
import csv
import itertools
import os
import random
import statistics

import numpy as np

import ml_benchmark as mb

gw = mb.gw
KINDS = ("normal", "speed", "night", "jump", "erratic")
TEST_SEEDS = (2, 3, 4, 5, 6)
TUNE_SEED = 1
TRIPS = 30   # per trip type per seed


def evaluate(profile_name, seed, contamination, envelope_factor):
    """Returns {kind: alert rate %} for one configuration on one seed's data."""
    profile = mb.PROFILES[profile_name]
    rng = random.Random(seed * 1000 + (0 if profile_name == "vehicle" else 1))
    train = mb.training_trips(profile, rng)
    det = mb.train_detector(train, contamination)
    envelope = None
    if envelope_factor:
        max_speed = np.vstack(det.movement_model.history)[:, 0].max()
        envelope = (envelope_factor * max_speed, gw.ENVELOPE_IMPLIED_FACTOR * envelope_factor * max_speed)
    out = {}
    for kind in KINDS:
        hits = 0
        for k in range(TRIPS):
            hour = rng.uniform(1.0, 4.0) if kind == "night" else rng.choice(
                [rng.uniform(6.5, 8.5), rng.uniform(16.0, 18.5)])
            trip = mb.make_trip(profile, rng, mb._epoch(mb.TRAIN_DAYS + k, hour), kind)
            _, _, pk, _, conf = mb.run_trip(det, trip, envelope)
            if kind == "normal":
                hits += any(conf)
            else:
                first = next(i for i, (_, t) in enumerate(trip) if t)
                end = first + gw.ML_CONFIRM_WINDOW if kind == "jump" else mb.TRIP_PACKETS
                hits += any(conf[first:end])
        out[kind] = 100.0 * hits / TRIPS
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", metavar="DIR")
    args = ap.parse_args()
    rows = []
    for prof in ("vehicle", "walking"):
        # ---- tuning on seed 1
        best = None
        for c, e in itertools.product((0.005, 0.01, 0.02, 0.05), (1.1, 1.25, 1.5)):
            r = evaluate(prof, TUNE_SEED, c, e)
            det_rate = statistics.mean(r[k] for k in ("speed", "jump", "erratic"))
            rows.append({"profile": prof, "phase": "tune", "seed": TUNE_SEED, "contamination": c,
                         "envelope": e, **{k: round(v, 1) for k, v in r.items()}})
            if det_rate >= 95 and (best is None or r["normal"] < best[2]["normal"]):
                best = (c, e, r)
        c_best, e_best, _ = best
        print(f"\n[{prof}] selected on tuning seed: contamination={c_best}, envelope={e_best}")
        # ---- testing on unseen seeds
        for label, c, e in (("baseline", 0.05, None), ("final", gw.ISOLATION_FOREST_CONTAMINATION, gw.ENVELOPE_SPEED_FACTOR), ("selected", c_best, e_best)):
            per_kind = {k: [] for k in KINDS}
            for s in TEST_SEEDS:
                r = evaluate(prof, s, c, e)
                rows.append({"profile": prof, "phase": "test-" + label, "seed": s, "contamination": c,
                             "envelope": e or "", **{k: round(v, 1) for k, v in r.items()}})
                for k in KINDS:
                    per_kind[k].append(r[k])
            summary = ", ".join(f"{k} {statistics.mean(v):.0f}±{statistics.stdev(v):.0f}%" for k, v in per_kind.items())
            print(f"  {label:<9} (c={c}, env={e}): {summary}")
    if args.csv:
        os.makedirs(args.csv, exist_ok=True)
        with open(os.path.join(args.csv, "ml_validation.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
