#!/usr/bin/env python3
"""
============================================================================
 Edge-Based Vehicle Theft Detection System — LoRa-GSM Hybrid Network
 DASHBOARD BACKEND (FastAPI) — multi-page live tracking + fleet management
============================================================================
Reads from the SAME SQLite database the gateway (pi_gateway.py) writes to
(see gateway/telemetry_store.py, which is imported directly rather than
duplicated — that module is the single source of truth for the schema).
This process only ever READS the database; it never writes to it, so it can
run fully independently of the gateway (start/stop/restart either one
without affecting the other).

Pages (server-rendered with Jinja2, sharing one dark-theme layout + nav):
    GET  /                          -> Live Map
    GET  /fleet                     -> Fleet list
    GET  /fleet/{node_id}           -> Vehicle detail
    GET  /alerts                    -> Alert / incident log
    GET  /analytics                 -> Fleet-wide analytics
    GET  /system                    -> System status

REST API (consumed by the pages' own JS, and reusable independently):
    GET  /api/nodes                     -> list of known vehicle node IDs
    GET  /api/latest?node_id=           -> latest telemetry row(s)
    GET  /api/history?node_id=&hours=   -> telemetry rows in a time window
    GET  /api/alerts?node_id=&limit=    -> recent confirmed alerts
    GET  /api/analytics/summary?hours=  -> per-vehicle aggregate stats
    GET  /api/system/status             -> gateway/link/DB health snapshot
    WS   /ws                            -> live push: snapshot, telemetry,
                                           alert, alert_update (SMS confirmed)

Live updates are done by polling the database for rows newer than the last
one already broadcast (every ~1.5s) rather than any direct link to the
gateway process — the two processes are fully decoupled and only agree on
the DB file.

Run (from the dashboard/ directory):
    uvicorn server:app --host 0.0.0.0 --port 8000
Then browse to http://<pi-ip-address>:8000/ from any device on the same
network.
============================================================================
"""

import asyncio
import logging
import math
import os
import sys
import time
from typing import List, Optional, Set

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# telemetry_store.py lives in the sibling gateway/ package and is the single
# source of truth for the DB schema — imported directly rather than
# duplicated here, so the dashboard can never drift out of sync with it.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway"))
import telemetry_store as store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("dashboard_server")

POLL_INTERVAL_SECONDS = 1.5
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STALE_AFTER_SECONDS = 900  # 15 min with no packet -> a vehicle/the link is "stale"
# A vehicle stays in the red "alert" state for this long after its most
# recent threat alert, instead of turning green again on the very next
# normal-looking packet (which would hide a theft in progress).
ALERT_ACTIVE_SECONDS = 1800
# The gateway rewrites its status row every ~10 s; older than this means the
# gateway process is not running.
GATEWAY_HEARTBEAT_STALE_SECONDS = 60
# How long to keep watching an unsent alert for its SMS to be confirmed.
SMS_WATCH_SECONDS = 3600

SERVER_STARTED_AT = time.time()

app = FastAPI(title="Vehicle Theft Detection Dashboard")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

NAV_ITEMS = [
    ("live_map", "/", "Live Map"),
    ("fleet", "/fleet", "Fleet"),
    ("alerts", "/alerts", "Alerts"),
    ("analytics", "/analytics", "Analytics"),
    ("system", "/system", "System"),
]


# Appended to CSS/JS URLs (?v=...) so browsers fetch fresh copies after the
# dashboard is updated and restarted, instead of mixing a cached old
# common.js with a new page script.
ASSET_VERSION = str(int(SERVER_STARTED_AT))


def render(request: Request, template_name: str, active: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, template_name, {
        "nav_items": NAV_ITEMS,
        "active": active,
        "asset_v": ASSET_VERSION,
        **context,
    })


# ---------------------------------------------------------------------------
# WebSocket connection registry
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self._connections: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        logger.info("Dashboard client connected (%d total).", len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        logger.info("Dashboard client disconnected (%d total).", len(self._connections))

    async def broadcast(self, message: dict) -> None:
        dead = []
        for ws in list(self._connections):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def count(self) -> int:
        return len(self._connections)


manager = ConnectionManager()


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def _enrich(row: dict) -> dict:
    """Attach what the pages need to show a vehicle's TRUE state, which the
    latest telemetry row alone can't tell them:
        last_valid_fix  where the vehicle was last seen with a fresh GPS fix
                        (the latest packet may be a stale-GPS motion packet)
        active_alert    its most recent threat alert within ALERT_ACTIVE_SECONDS
    Blocking (SQLite) — call via asyncio.to_thread."""
    fix = store.get_last_valid_fix(row["node_id"])
    since = time.time() - ALERT_ACTIVE_SECONDS
    alert = store.get_latest_threat_alert(row["node_id"], since)
    if alert is not None and alert["reason"] == "loss_of_contact" and row["received_at"] > alert["sent_at"]:
        # Contact has been restored since that alert; an earlier THEFT alert
        # in the window must still keep the vehicle red.
        theft = store.get_latest_threat_alert(row["node_id"], since, exclude_contact=True)
        if theft is not None:
            alert = theft
    row["last_valid_fix"] = _row_to_dict(fix) if fix else None
    row["active_alert"] = _row_to_dict(alert) if alert else None
    return row


def _latest_enriched(node_id: Optional[int] = None) -> List[dict]:
    return [_enrich(_row_to_dict(r)) for r in store.get_latest_telemetry(node_id)]


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


MOVING_SPEED_KMPH = 3.0      # below this the vehicle is treated as parked
GPS_JITTER_KM = 0.03         # hops shorter than this while parked are GPS noise


def _packet_delivery_ratio(rows: List[dict]) -> Optional[dict]:
    """Packet Delivery Ratio from the ESP32's per-packet sequence numbers:
    received / expected, where 'expected' is the span of sequence numbers
    seen. A sequence number that goes backwards marks a node restart and
    starts a new segment. Rows from legacy (v1) firmware have no sequence
    number and are ignored. Returns None if there is nothing to measure."""
    seqs = [r["sequence_number"] for r in rows if r.get("sequence_number") is not None]
    if not seqs:
        return None
    expected = received = 0
    segment: Set[int] = set()
    first = prev = seqs[0]
    for seq in seqs:
        if seq < prev:                       # node restarted: close the segment
            expected += prev - first + 1
            received += len(segment)
            segment, first = set(), seq
        segment.add(seq)
        prev = seq
    expected += prev - first + 1
    received += len(segment)
    if expected <= 0:
        return None
    return {"received": received, "expected": expected,
            "pdr_percent": round(100.0 * received / expected, 1)}


# ---------------------------------------------------------------------------
# Background poller — bridges "gateway wrote a new row" to "clients notified"
# ---------------------------------------------------------------------------
async def _poll_for_changes() -> None:
    cursors = await asyncio.to_thread(store.get_max_ids)
    last_telemetry_id = cursors["telemetry"]
    last_alert_id = cursors["alerts"]
    logger.info("Change-poller starting from telemetry id=%d, alert id=%d",
                last_telemetry_id, last_alert_id)

    # The gateway writes an alert row BEFORE its SMS goes out and flips
    # sms_sent afterwards (possibly minutes later, if the modem was down).
    # New-row polling alone would never show that change, so unsent recent
    # alerts are watched and an "alert_update" is pushed when they're sent.
    unsent: dict = {}   # alert id -> sent_at
    recent = await asyncio.to_thread(store.get_recent_alerts, None, 200)
    for r in recent:
        if not r["sms_sent"] and time.time() - r["sent_at"] < SMS_WATCH_SECONDS:
            unsent[r["id"]] = r["sent_at"]

    while True:
        try:
            new_telemetry = await asyncio.to_thread(store.get_telemetry_after_id, last_telemetry_id)
            for row in new_telemetry:
                data = await asyncio.to_thread(_enrich, _row_to_dict(row))
                await manager.broadcast({"type": "telemetry", "data": data})
                last_telemetry_id = row["id"]

            new_alerts = await asyncio.to_thread(store.get_alerts_after_id, last_alert_id)
            for row in new_alerts:
                await manager.broadcast({"type": "alert", "data": _row_to_dict(row)})
                last_alert_id = row["id"]
                if not row["sms_sent"]:
                    unsent[row["id"]] = row["sent_at"]

            if unsent:
                now = time.time()
                for alert_id in [i for i, t in unsent.items() if now - t > SMS_WATCH_SECONDS]:
                    del unsent[alert_id]
                rows = await asyncio.to_thread(store.get_alerts_by_ids, list(unsent))
                for row in rows:
                    if row["sms_sent"]:
                        del unsent[row["id"]]
                        await manager.broadcast({"type": "alert_update", "data": _row_to_dict(row)})
        except Exception:
            logger.exception("Error while polling for DB changes.")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@app.on_event("startup")
async def on_startup() -> None:
    store.init_db()  # harmless no-op if the gateway already created it
    asyncio.create_task(_poll_for_changes())


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def page_live_map(request: Request):
    return render(request, "live_map.html", "live_map")


@app.get("/fleet", response_class=HTMLResponse)
async def page_fleet(request: Request):
    return render(request, "fleet.html", "fleet")


@app.get("/fleet/{node_id}", response_class=HTMLResponse)
async def page_vehicle_detail(request: Request, node_id: int):
    return render(request, "vehicle_detail.html", "fleet", node_id=node_id)


@app.get("/alerts", response_class=HTMLResponse)
async def page_alerts(request: Request):
    return render(request, "alerts.html", "alerts")


@app.get("/analytics", response_class=HTMLResponse)
async def page_analytics(request: Request):
    return render(request, "analytics.html", "analytics")


@app.get("/system", response_class=HTMLResponse)
async def page_system(request: Request):
    return render(request, "system.html", "system")


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------
@app.get("/api/nodes")
async def api_nodes() -> List[int]:
    return await asyncio.to_thread(store.get_known_node_ids)


@app.get("/api/latest")
async def api_latest(node_id: Optional[int] = None):
    """Latest row per vehicle, plus its last valid GPS fix and any active
    alert (see _enrich)."""
    return await asyncio.to_thread(_latest_enriched, node_id)


@app.get("/api/history")
async def api_history(node_id: int, hours: float = 24.0, limit: int = 5000):
    since_epoch = time.time() - hours * 3600.0
    rows = await asyncio.to_thread(store.get_telemetry_since, node_id, since_epoch, limit)
    return [_row_to_dict(r) for r in rows]


@app.get("/api/alerts")
async def api_alerts(node_id: Optional[int] = None, limit: int = 100):
    rows = await asyncio.to_thread(store.get_recent_alerts, node_id, limit)
    return [_row_to_dict(r) for r in rows]


@app.get("/api/analytics/summary")
async def api_analytics_summary(hours: float = 24.0):
    """Per-vehicle aggregate stats over the requested window: average speed
    while moving, peak speed, an approximate distance travelled (summed
    haversine distance between consecutive valid GPS fixes, ignoring GPS
    jitter while parked), packet count, anomaly count, average signal, and
    Packet Delivery Ratio from the node's sequence numbers."""
    node_ids = await asyncio.to_thread(store.get_known_node_ids)
    since_epoch = time.time() - hours * 3600.0
    summary = []
    for node_id in node_ids:
        rows = await asyncio.to_thread(store.get_telemetry_since, node_id, since_epoch, 20000)
        rows = [_row_to_dict(r) for r in rows]
        if not rows:
            summary.append({
                "node_id": node_id, "packet_count": 0, "avg_speed_kmph": 0.0,
                "max_speed_kmph": 0.0, "distance_km": 0.0, "anomaly_count": 0,
                "avg_rssi_dbm": None, "pdr": None,
            })
            continue

        valid = [r for r in rows if r["gps_valid"]]
        # Average over MOVING samples only — a parked car's heartbeats (all
        # 0 km/h) would otherwise drag the average towards zero.
        moving_speeds = [r["speed_kmph"] for r in valid if r["speed_kmph"] >= MOVING_SPEED_KMPH]
        distance_km = 0.0
        for a, b in zip(valid, valid[1:]):
            hop = _haversine_km(a["latitude"], a["longitude"], b["latitude"], b["longitude"])
            parked = a["speed_kmph"] < MOVING_SPEED_KMPH and b["speed_kmph"] < MOVING_SPEED_KMPH
            if parked and hop < GPS_JITTER_KM:
                continue
            distance_km += hop
        rssi_values = [r["rssi_dbm"] for r in rows if r.get("rssi_dbm") is not None]

        summary.append({
            "node_id": node_id,
            "packet_count": len(rows),
            "avg_speed_kmph": round(sum(moving_speeds) / len(moving_speeds), 1) if moving_speeds else 0.0,
            "max_speed_kmph": round(max(r["speed_kmph"] for r in rows), 1),
            "distance_km": round(distance_km, 2),
            "anomaly_count": sum(1 for r in rows if r["is_anomaly"]),
            "avg_rssi_dbm": round(sum(rssi_values) / len(rssi_values), 1) if rssi_values else None,
            "pdr": _packet_delivery_ratio(rows),
        })
    return {"hours": hours, "vehicles": summary}


@app.get("/api/system/status")
async def api_system_status():
    node_ids = await asyncio.to_thread(store.get_known_node_ids)
    latest_rows = await asyncio.to_thread(store.get_latest_telemetry, None)
    latest_rows = [_row_to_dict(r) for r in latest_rows]
    counts = await asyncio.to_thread(store.get_counts)
    recent_alerts = await asyncio.to_thread(store.get_recent_alerts, None, 1)
    recent_alerts = [_row_to_dict(r) for r in recent_alerts]

    now = time.time()
    most_recent_packet_age = None
    if latest_rows:
        most_recent_packet_age = now - max(r["received_at"] for r in latest_rows)
    lora_link_status = "unknown"
    if most_recent_packet_age is not None:
        lora_link_status = "linked" if most_recent_packet_age < STALE_AFTER_SECONDS else "stale"

    db_path = store.DEFAULT_DB_PATH
    db_size_bytes = os.path.getsize(db_path) if os.path.exists(db_path) else 0

    last_alert = recent_alerts[0] if recent_alerts else None

    gw_row = await asyncio.to_thread(store.get_gateway_status)
    gateway = None
    if gw_row is not None:
        gateway = _row_to_dict(gw_row)
        age = now - gateway["updated_at"]
        gateway["seconds_since_heartbeat"] = round(age)
        gateway["running"] = age < GATEWAY_HEARTBEAT_STALE_SECONDS
        gateway["uptime_seconds"] = round(now - gateway["started_at"]) if gateway["running"] else None

    return {
        "gateway": gateway,
        "dashboard_uptime_seconds": round(now - SERVER_STARTED_AT),
        "connected_dashboard_clients": manager.count,
        "known_vehicle_count": len(node_ids),
        "lora_link_status": lora_link_status,
        "seconds_since_last_packet": round(most_recent_packet_age) if most_recent_packet_age is not None else None,
        "telemetry_row_count": counts["telemetry"],
        "alert_row_count": counts["alerts"],
        "db_path": db_path,
        "db_size_bytes": db_size_bytes,
        "last_alert": last_alert,
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
    }


# ---------------------------------------------------------------------------
# WebSocket endpoint (shared by every page's live-update JS)
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        # Send an initial snapshot so a freshly-opened page isn't blank until
        # the next telemetry packet arrives.
        latest_rows = await asyncio.to_thread(_latest_enriched, None)
        recent_alerts = await asyncio.to_thread(store.get_recent_alerts, None, 20)
        await websocket.send_json({
            "type": "snapshot",
            "data": {
                "latest": latest_rows,
                "alerts": [_row_to_dict(r) for r in recent_alerts],
            },
        })
        while True:
            # We don't expect client -> server messages, but keep the socket
            # alive and detect disconnects promptly.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)


# ---------------------------------------------------------------------------
# Static assets (css/js)
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
