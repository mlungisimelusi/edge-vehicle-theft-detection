# Phase 4 — Live Dashboard Setup

A multi-page live tracking and fleet-management web application, served from the Raspberry Pi 5 itself, viewable from any browser on the same network. Built as a second, independent process from the gateway — they only ever agree on one thing: the SQLite database file.

---

## 1. Architecture, in short

```
ESP32 --LoRa--> pi_gateway.py --writes--> SQLite (gateway/data/vehicle_theft_detection.db)
                                                |
                                        dashboard/server.py --reads-->
                                                |
                                     FastAPI (REST + WebSocket + Jinja2 pages)
                                                |
                    dashboard/templates/*.html (5 real pages, shared layout) + static/css, static/js
```

`pi_gateway.py` never imports or talks to the dashboard directly, and `dashboard/server.py` never writes to the database — it only reads. This means you can restart either one independently, and if the dashboard crashes or you haven't started it yet, the gateway keeps working (anomaly detection and SMS alerts don't depend on the dashboard at all).

The dashboard backend detects new rows by polling the database every 1.5 seconds (`POLL_INTERVAL_SECONDS` in `server.py`) rather than any direct hand-off from the gateway — simple, and it means the dashboard would work identically even if you swapped the gateway for something else that writes to the same schema.

### Pages

The dashboard is a real multi-page site, server-rendered with FastAPI + Jinja2, not a single HTML file:

| Route | Template | Purpose |
|---|---|---|
| `/` | `live_map.html` | Live Leaflet map, animated status-coloured vehicle markers, compact fleet sidebar |
| `/fleet` | `fleet.html` | Sortable/searchable table of every known vehicle |
| `/fleet/{node_id}` | `vehicle_detail.html` | One vehicle's live stats, position, alert history, speed/battery charts |
| `/alerts` | `alerts.html` | Full incident log across the fleet, filterable by vehicle |
| `/analytics` | `analytics.html` | Fleet-wide aggregates: avg/max speed, distance travelled, anomaly counts per vehicle |
| `/system` | `system.html` | Gateway/link health, DB size and row counts, most recent alert |

All six share one dark-theme layout (`templates/base.html`) and one WebSocket connection (`static/js/common.js`) — each page's own script only subscribes to the message types it actually needs (`static/js/live_map.js`, `fleet.js`, `vehicle_detail.js`, `alerts.js`, `analytics.js`, `system.js`).

## 2. Install

```bash
cd 02_Edge_Gateway_RPi5/dashboard
python3 -m venv venv          # can reuse the gateway's venv instead if you prefer
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Run it

With the gateway already running (see `Phase3_Setup.md`) in one terminal/service, start the dashboard in another:

```bash
cd 02_Edge_Gateway_RPi5/dashboard
uvicorn server:app --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` is what makes it reachable from other devices on the network, not just the Pi itself. Then, from a laptop/phone on the same WiFi:

```
http://<pi-ip-address>:8000/
```

Find the Pi's IP with `hostname -I` on the Pi itself.

## 4. What you'll see

Every page uses the same vehicle states (defined once in `static/js/common.js`):

| State | Colour | Meaning |
|---|---|---|
| **theft alert** | red | The gateway raised a theft alert in the last 30 minutes. It stays red for that long, so a stolen car doesn't turn green again on its next normal-looking packet. |
| **contact lost** | red | A loss-of-contact alert was raised and the vehicle still hasn't reported. |
| **no signal** | amber | No packet for 15 minutes. |
| **gps lost** / **no gps fix** | amber | Packets are arriving, but there's been no fresh GPS fix for 10 minutes (or ever). |
| **live** | green | Reporting, with a recent GPS fix. |

States are based on alerts, not on each packet's `is_anomaly` flag (with 5% contamination the model flags the odd normal packet; an alert is what the gateway actually acted on). Pages re-check state every 15 seconds, so a vehicle that goes silent turns amber even though no new message arrives.

**Live Map** (`/`) — a Leaflet map with a base-map switcher in the top-right corner (Dark, Streets and Satellite; OpenStreetMap and Esri tiles, no API key needed, and your choice is remembered) with a rotating, coloured marker per vehicle and its route over the last 2 hours. A vehicle whose latest packet has no GPS fix (for example the immediate motion packet) stays on the map at its last known position instead of disappearing. A compact sidebar lists every vehicle with its state and a link to its detail page.

**Fleet** (`/fleet`) — every vehicle the gateway has heard from, in a searchable table (state, speed, battery in volts, satellites, signal, last GPS fix, last packet), with vehicles in trouble sorted to the top. Click a row to open the detail page.

**Vehicle detail** (`/fleet/{id}`) — one vehicle's current stats, last known position (with its age, coordinates and a Google Maps link), its route over the chosen window, its alert history, and speed/battery charts over 1h/24h/7d. Speed is plotted only from packets with a fresh fix, so stale packets don't draw false "stopped" dips. This is what you'd screenshot for your report's evaluation section — actual collected data, not a mockup.

**Alerts** (`/alerts`) — the full incident log across every vehicle, filterable by vehicle, with readable reasons and a Google Maps link to the last known location. An alert shows "SMS pending" until the gateway confirms the SMS went out, then updates live to "SMS sent".

**Analytics** (`/analytics`) — fleet-wide aggregates over a chosen window: Packet Delivery Ratio, average moving speed, peak speed, approximate distance travelled (ignoring GPS jitter while parked), and anomaly counts per vehicle, as both charts and a table.

**System** (`/system`) — whether the **gateway process** is running (from a heartbeat it writes every 10 seconds), whether **SMS alerts** can be sent (SIM800C ready, alert numbers configured), how many alerts are waiting for SMS, LoRa packets and CRC errors, plus link freshness, database size/row counts and the most recent alert. It's a health check of the whole pipeline without reading gateway logs.

## 5. Run automatically on boot (optional)

Same pattern as the gateway's service. Create `/etc/systemd/system/vehicle-dashboard.service`:

```ini
[Unit]
Description=Vehicle Theft Detection Dashboard
After=network.target vehicle-gateway.service

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/path/to/02_Edge_Gateway_RPi5/dashboard
ExecStart=/home/YOUR_USERNAME/path/to/02_Edge_Gateway_RPi5/dashboard/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vehicle-dashboard.service
```

## 6. Notes

- This is intentionally unauthenticated and local-network-only — see the earlier discussion on why (no internet exposure for a live vehicle-tracking interface without a real auth story). If you ever want it reachable outside your network, put it behind Tailscale rather than opening a port to the internet.
- The dashboard reads via `sys.path` import of `gateway/telemetry_store.py` rather than duplicating the schema — if you ever change a column there, the dashboard picks it up automatically since it's the same code, not a copy.
- `MAX_LIVE_TRAIL_POINTS` (in `static/js/live_map.js`) caps how many live points are kept in the browser's in-memory trail before older ones drop off, so a long-running dashboard tab doesn't slowly accumulate unbounded memory. History views always re-fetch the full window from the database regardless.
- `/api/analytics/summary` and `/api/system/status` are new REST endpoints added for the Analytics and System pages — both are read-only aggregates over the same `telemetry_store.py` functions the other pages already use, so they carry no extra risk to the write path.
- `/api/latest` and the WebSocket attach two extra fields to each vehicle row: `last_valid_fix` (last position with a fresh GPS fix) and `active_alert` (most recent threat alert in the last 30 minutes). The WebSocket also pushes an `alert_update` message when an alert's SMS is confirmed.
- The dashboard is still read-only. The only thing the gateway adds for it is the single-row `gateway_status` heartbeat table.
- CSS/JS URLs carry a `?v=` version that changes on every dashboard restart, so browsers never mix a cached old script with new pages after an update.
- `dashboard/static/index.html` is no longer served by any route (kept only as a pointer to the new template structure) — the old single-page dashboard has been fully replaced by the multi-page app described above.
