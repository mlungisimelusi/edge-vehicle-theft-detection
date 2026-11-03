#!/usr/bin/env python3
"""
============================================================================
 Edge-Based Vehicle Theft Detection System — EVALUATION SESSIONS & REPORT
============================================================================
Collects the evidence for RO3 (detection responsiveness and failover
performance) while the real gateway runs, then turns it into tables, CSV
files and charts for the results chapter.

You label each test with a start/stop pair while the gateway is running (in
a second SSH window). The gateway already records every packet and alert
with timestamps, so the report can measure everything else automatically.

    python eval_session.py start theft  --note "door opened, car pushed"
    python eval_session.py sms          # the moment the SMS arrives on your phone
    python eval_session.py stop

Test types (labels):
    theft    the vehicle is moved/driven as a thief would. Detected if a
             theft alert is raised in the session. Do some ARMED (tests the
             motion rule) and some DISARMED (tests the Isolation Forest).
    normal   ordinary owner use, DISARMED. Any theft alert = false alarm.
    range    node held at a fixed distance: --distance 50 (metres). Gives
             packet delivery ratio, RSSI and SNR per distance.
    shield   node shielded (metal tin) or carried out of range while ARMED,
             to test failover: time until the CONTACT LOST alert.

Other commands:
    python eval_session.py list
    python eval_session.py report                 # writes results/ next to this file
    python eval_session.py report --out D:/results --node 1001

Measured automatically:
    detection rate (armed / disarmed), false-alarm rate, detection latency
    (packet received -> alert raised), SMS dispatch latency (alert -> SMS
    accepted by the network), end-to-end latency (motion packet -> SMS on
    your phone, from the 'sms' command), PDR / RSSI / SNR per distance,
    loss-of-contact detection time, SMS delivery ratio.
============================================================================
"""

import argparse
import csv
import json
import os
import statistics
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import telemetry_store as store  # noqa: E402

LABELS = ("theft", "normal", "range", "shield")
THEFT_REASONS = {"armed_motion", "isolation_forest", "speed_envelope", "isolation_forest_context", "coldstart_hardware_motion_flag"}
DETECTION_GRACE_S = 120      # alerts up to 2 min after 'stop' still count for a theft test
SHIELD_GRACE_S = 120


def _fmt(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"


def _default_armed() -> bool:
    try:
        with open(os.path.join(HERE, "gateway_config.json"), encoding="utf-8") as fh:
            return bool(json.load(fh).get("default_armed", True))
    except (OSError, json.JSONDecodeError):
        return True


def _stats(values: List[float]) -> Dict[str, Optional[float]]:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None, "sd": None}
    return {"n": len(values), "mean": statistics.mean(values), "median": statistics.median(values),
            "min": min(values), "max": max(values),
            "sd": statistics.stdev(values) if len(values) > 1 else 0.0}


def _num(v, digits=1) -> str:
    return "-" if v is None else f"{v:.{digits}f}"


def _pdr(rows) -> Optional[float]:
    pairs = [(r["boot_epoch"], r["sequence_number"]) for r in rows if r["sequence_number"] is not None]
    if not pairs:
        return None
    expected = received = 0
    seen = set()
    epoch0, first = pairs[0]
    prev = first
    for epoch, seq in pairs:
        if epoch != epoch0 or seq < prev:
            expected += prev - first + 1
            received += len(seen)
            seen, first, epoch0 = set(), seq, epoch
        seen.add(seq)
        prev = seq
    expected += prev - first + 1
    received += len(seen)
    return 100.0 * received / expected if expected else None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_start(args) -> int:
    store.init_db()
    open_event = store.get_open_evaluation_event()
    if open_event is not None:
        store.end_evaluation_event(open_event["id"])
        print(f"(closed the previous open session #{open_event['id']} '{open_event['label']}')")
    if args.label == "range" and args.distance is None:
        print("A range test needs --distance in metres, e.g.  start range --distance 50")
        return 1
    armed = store.get_vehicle_armed(args.node, _default_armed())
    event_id = store.start_evaluation_event(args.label, args.distance, armed, args.note)
    print(f"Session #{event_id} STARTED: {args.label}"
          f"{f' at {args.distance:g} m' if args.distance is not None else ''}"
          f" — vehicle {args.node} is {'ARMED' if armed else 'DISARMED'} — {_fmt(time.time())}")
    if args.label == "normal" and armed:
        print("Warning: a 'normal' (owner use) test should be done DISARMED; while armed, any motion alerts.")
    if args.label == "shield" and not armed:
        print("Warning: loss-of-contact alerts are only sent while ARMED. Arm the vehicle first.")
    print("Run 'python eval_session.py stop' when the test is over.")
    return 0


def cmd_stop(args) -> int:
    open_event = store.get_open_evaluation_event()
    if open_event is None:
        print("No session is running.")
        return 1
    store.end_evaluation_event(open_event["id"])
    print(f"Session #{open_event['id']} '{open_event['label']}' STOPPED — {_fmt(time.time())}")
    return 0


def cmd_sms(args) -> int:
    event = store.get_open_evaluation_event()
    if event is None:
        events = store.get_evaluation_events()
        event = events[-1] if events else None
    if event is None:
        print("No session to attach the SMS time to.")
        return 1
    now = time.time()
    store.set_evaluation_phone_time(event["id"], now)
    print(f"SMS arrival on phone recorded for session #{event['id']} at {_fmt(now)}.")
    return 0


def cmd_list(args) -> int:
    events = store.get_evaluation_events()
    if not events:
        print("No sessions recorded yet.")
        return 0
    print(f"{'#':>4}  {'label':<7} {'mode':<9} {'distance':>8}  {'start':<19}  {'end':<19}  note")
    for e in events:
        mode = "-" if e["armed"] is None else ("ARMED" if e["armed"] else "disarmed")
        dist = "-" if e["distance_m"] is None else f"{e['distance_m']:g} m"
        print(f"{e['id']:>4}  {e['label']:<7} {mode:<9} {dist:>8}  {_fmt(e['started_at']):<19}  "
              f"{_fmt(e['ended_at']):<19}  {e['note'] or ''}")
    return 0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def analyse(node: int) -> dict:
    events = [e for e in store.get_evaluation_events() if e["ended_at"] is not None]
    theft, normal, ranges, shields = [], [], [], []

    for e in events:
        start, end = e["started_at"], e["ended_at"]
        if e["label"] == "theft":
            packets = store.get_telemetry_between(start, end + DETECTION_GRACE_S, node)
            motion = [p for p in packets if p["motion_flag"]]
            alerts = [a for a in store.get_alerts_between(start, end + DETECTION_GRACE_S, node)
                      if a["reason"] in THEFT_REASONS]
            first_motion = motion[0]["received_at"] if motion else None
            a = alerts[0] if alerts else None
            theft.append({
                "session": e["id"], "mode": "armed" if e["armed"] else "disarmed", "note": e["note"] or "",
                "detected": bool(a), "reason": a["reason"] if a else "",
                "detection_s": (a["sent_at"] - a["trigger_received_at"]) if a and a["trigger_received_at"] else None,
                "motion_to_alert_s": (a["sent_at"] - first_motion) if a and first_motion else None,
                "sms_dispatch_s": (a["sms_sent_at"] - a["sent_at"]) if a and a["sms_sent_at"] else None,
                "motion_to_phone_s": (e["phone_sms_at"] - first_motion) if e["phone_sms_at"] and first_motion else None,
                "sms_sent": bool(a and a["sms_sent"]),
            })
        elif e["label"] == "normal":
            alerts = [a for a in store.get_alerts_between(start, end, node) if a["reason"] in THEFT_REASONS]
            hours = max((end - start) / 3600.0, 1e-9)
            normal.append({"session": e["id"], "mode": "armed" if e["armed"] else "disarmed",
                           "note": e["note"] or "", "minutes": (end - start) / 60.0,
                           "false_alarms": len(alerts), "false_alarm": bool(alerts),
                           "alerts_per_hour": len(alerts) / hours})
        elif e["label"] == "range":
            packets = store.get_telemetry_between(start, end, node)
            rssi = [p["rssi_dbm"] for p in packets]
            snr = [p["snr_db"] for p in packets]
            ranges.append({"session": e["id"], "distance_m": e["distance_m"], "packets": len(packets),
                           "pdr_percent": _pdr(packets),
                           "rssi_mean": statistics.mean(rssi) if rssi else None,
                           "rssi_min": min(rssi) if rssi else None,
                           "snr_mean": statistics.mean(snr) if snr else None})
        elif e["label"] == "shield":
            alerts = [a for a in store.get_alerts_between(start, end + SHIELD_GRACE_S, node)
                      if a["reason"] == "loss_of_contact"]
            a = alerts[0] if alerts else None
            # The silence starts at the last packet that got through before the
            # alert (or before the session started, if no alert was raised).
            before = store.get_telemetry_between(start - 3600, a["sent_at"] if a else start, node)
            last_packet = before[-1]["received_at"] if before else None
            after = store.get_telemetry_between(end, end + 3600, node)
            shields.append({"session": e["id"], "mode": "armed" if e["armed"] else "disarmed",
                            "contact_lost_alert": bool(a),
                            "silence_to_alert_s": (a["sent_at"] - last_packet) if a and last_packet else None,
                            "sms_sent": bool(a and a["sms_sent"]),
                            "recovery_s": (after[0]["received_at"] - end) if after else None})

    all_alerts = [a for a in store.get_all_alerts() if a["node_id"] == node]
    dispatch = [a["sms_sent_at"] - a["sent_at"] for a in all_alerts if a["sms_sent_at"]]
    return {"events": events, "theft": theft, "normal": normal, "range": ranges, "shield": shields,
            "alerts": all_alerts, "dispatch": dispatch}


def _write_csv(path: str, rows: List[dict]) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _charts(out: str, r: dict) -> List[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    made = []
    lat = {
        "Packet -> alert": [t["detection_s"] for t in r["theft"] if t["detection_s"] is not None],
        "Alert -> SMS sent": [t["sms_dispatch_s"] for t in r["theft"] if t["sms_dispatch_s"] is not None],
        "Motion -> SMS on phone": [t["motion_to_phone_s"] for t in r["theft"] if t["motion_to_phone_s"] is not None],
    }
    lat = {k: v for k, v in lat.items() if v}
    if lat:
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.boxplot(list(lat.values()), labels=list(lat.keys()))
        ax.set_ylabel("seconds")
        ax.set_title("Alert latency")
        ax.grid(axis="y", alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(out, "latency.png"), dpi=150)
        plt.close(fig)
        made.append("latency.png")
    rng = sorted([x for x in r["range"] if x["distance_m"] is not None], key=lambda x: x["distance_m"])
    if rng:
        d = [x["distance_m"] for x in rng]
        fig, ax1 = plt.subplots(figsize=(7, 4))
        ax1.plot(d, [x["pdr_percent"] if x["pdr_percent"] is not None else 0 for x in rng], "o-", color="tab:blue")
        ax1.set_xlabel("distance (m)")
        ax1.set_ylabel("packet delivery ratio (%)", color="tab:blue")
        ax1.set_ylim(0, 105)
        ax2 = ax1.twinx()
        ax2.plot(d, [x["rssi_mean"] if x["rssi_mean"] is not None else float("nan") for x in rng],
                 "s--", color="tab:red")
        ax2.set_ylabel("mean RSSI (dBm)", color="tab:red")
        ax1.set_title("LoRa link vs distance")
        ax1.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(out, "range.png"), dpi=150)
        plt.close(fig)
        made.append("range.png")
    return made


def cmd_report(args) -> int:
    store.init_db()
    out = args.out or os.path.join(HERE, "results")
    os.makedirs(out, exist_ok=True)
    r = analyse(args.node)
    L = []
    L.append(f"# Evaluation report — vehicle {args.node}")
    L.append(f"Generated {_fmt(time.time())} from {store.DEFAULT_DB_PATH}\n")
    L.append(f"Sessions analysed: {len(r['events'])} "
             f"(theft {len(r['theft'])}, normal {len(r['normal'])}, range {len(r['range'])}, "
             f"shield {len(r['shield'])})\n")

    L.append("## 1. Theft detection")
    for mode in ("armed", "disarmed"):
        rows = [t for t in r["theft"] if t["mode"] == mode]
        if rows:
            det = sum(t["detected"] for t in rows)
            L.append(f"- **{mode.capitalize()}**: {det} of {len(rows)} theft tests detected "
                     f"({100.0 * det / len(rows):.1f}%)")
    if r["theft"]:
        L.append("")
        L.append("| Latency (s) | n | mean | median | min | max | SD |")
        L.append("|---|---|---|---|---|---|---|")
        for name, key in (("Packet received → alert raised (gateway processing)", "detection_s"),
                          ("First motion packet → alert raised", "motion_to_alert_s"),
                          ("Alert raised → SMS accepted by network", "sms_dispatch_s"),
                          ("First motion packet → SMS on phone (end-to-end)", "motion_to_phone_s")):
            s = _stats([t[key] for t in r["theft"]])
            L.append(f"| {name} | {s['n']} | {_num(s['mean'], 2)} | {_num(s['median'], 2)} | "
                     f"{_num(s['min'], 2)} | {_num(s['max'], 2)} | {_num(s['sd'], 2)} |")
    else:
        L.append("No theft sessions recorded.")
    L.append("")

    L.append("## 2. False alarms during normal (disarmed) use")
    if r["normal"]:
        fa = sum(n["false_alarm"] for n in r["normal"])
        hours = sum(n["minutes"] for n in r["normal"]) / 60.0
        total = sum(n["false_alarms"] for n in r["normal"])
        L.append(f"- {fa} of {len(r['normal'])} normal-use sessions raised a false alarm "
                 f"({100.0 * fa / len(r['normal']):.1f}%)")
        L.append(f"- {total} false alarm(s) in {hours:.2f} h of normal use "
                 f"({total / hours if hours else 0:.2f} per hour)")
    else:
        L.append("No normal-use sessions recorded.")
    L.append("")

    L.append("## 3. LoRa link vs distance")
    if r["range"]:
        L.append("| Distance (m) | Packets | PDR (%) | Mean RSSI (dBm) | Min RSSI (dBm) | Mean SNR (dB) |")
        L.append("|---|---|---|---|---|---|")
        for x in sorted(r["range"], key=lambda x: x["distance_m"] or 0):
            L.append(f"| {x['distance_m']:g} | {x['packets']} | {_num(x['pdr_percent'])} | "
                     f"{_num(x['rssi_mean'])} | {_num(x['rssi_min'], 0)} | {_num(x['snr_mean'])} |")
    else:
        L.append("No range sessions recorded.")
    L.append("")

    L.append("## 4. Failover: loss of contact (shield / out of range)")
    if r["shield"]:
        ok = sum(s["contact_lost_alert"] for s in r["shield"])
        st = _stats([s["silence_to_alert_s"] for s in r["shield"]])
        rec = _stats([s["recovery_s"] for s in r["shield"]])
        L.append(f"- CONTACT LOST alert raised in {ok} of {len(r['shield'])} tests "
                 f"({100.0 * ok / len(r['shield']):.1f}%)")
        L.append(f"- Last packet → CONTACT LOST alert: mean {_num(st['mean'])} s "
                 f"(min {_num(st['min'])}, max {_num(st['max'])})")
        L.append(f"- Shield removed → first packet again: mean {_num(rec['mean'])} s")
    else:
        L.append("No shield sessions recorded.")
    L.append("")

    L.append("## 5. All alerts for this vehicle")
    by_reason: Dict[str, int] = {}
    for a in r["alerts"]:
        by_reason[a["reason"]] = by_reason.get(a["reason"], 0) + 1
    sent = sum(1 for a in r["alerts"] if a["sms_sent"])
    L.append(f"- {len(r['alerts'])} alert(s): " + ", ".join(f"{k} {v}" for k, v in sorted(by_reason.items())))
    if r["alerts"]:
        L.append(f"- SMS delivered to the network for {sent} of {len(r['alerts'])} "
                 f"({100.0 * sent / len(r['alerts']):.1f}%)")
    ds = _stats(r["dispatch"])
    L.append(f"- SMS dispatch time (alert → network accepted): mean {_num(ds['mean'], 2)} s, "
             f"max {_num(ds['max'], 2)} s (n={ds['n']})")

    _write_csv(os.path.join(out, "theft_tests.csv"), r["theft"])
    _write_csv(os.path.join(out, "normal_tests.csv"), r["normal"])
    _write_csv(os.path.join(out, "range_tests.csv"), r["range"])
    _write_csv(os.path.join(out, "shield_tests.csv"), r["shield"])
    charts = _charts(out, r)
    if charts:
        L.append("")
        L.append("## Charts")
        L.extend(f"![{c}]({c})" for c in charts)
    else:
        L.append("\n(Install matplotlib for charts: pip install matplotlib)")

    report = "\n".join(L) + "\n"
    with open(os.path.join(out, "evaluation_report.md"), "w", encoding="utf-8") as fh:
        fh.write(report)
    print(report)
    print(f"Saved to {out}: evaluation_report.md, CSV files" + (", " + ", ".join(charts) if charts else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Label evaluation tests and build the results report.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--node", type=int, default=1001, help="vehicle node ID (default 1001)")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("start", help="start a labelled test session")
    p.add_argument("label", choices=LABELS)
    p.add_argument("--distance", type=float, help="metres from the gateway (range tests)")
    p.add_argument("--note", help="free-text description")
    sub.add_parser("stop", help="end the running session")
    sub.add_parser("sms", help="record the moment the SMS arrived on your phone")
    sub.add_parser("list", help="list recorded sessions")
    p = sub.add_parser("report", help="analyse all sessions and write the report")
    p.add_argument("--out", help="output folder (default: gateway/results)")
    args = ap.parse_args()
    store.init_db()
    return {"start": cmd_start, "stop": cmd_stop, "sms": cmd_sms, "list": cmd_list,
            "report": cmd_report}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
