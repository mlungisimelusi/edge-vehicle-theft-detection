"""
============================================================================
 Edge-Based Vehicle Theft Detection System — LoRa-GSM Hybrid Network
 SQLite telemetry/alert store — shared data layer for the gateway + dashboard
============================================================================
This is the single source of truth for everything the gateway records:
every received telemetry packet, and every confirmed anomaly alert.

WHY SQLITE: this is a low-throughput IoT feed (one packet every few seconds
to minutes per vehicle, from one or a handful of vehicles) — a full
client/server database would be pure overhead. SQLite gives proper querying
(history windows, per-node filtering, ordering) with zero setup, as a single
file on disk.

TWO-PROCESS ACCESS PATTERN: the gateway (pi_gateway.py) WRITES here on every
received packet/alert. The dashboard backend (dashboard/server.py) READS
from the same file in a separate process. SQLite's WAL (Write-Ahead Log)
journal mode is enabled specifically so the dashboard can keep reading while
the gateway is writing, without either process blocking the other.

Each function opens and closes its own short-lived connection rather than
sharing one across threads/processes — at this data rate the overhead is
irrelevant, and it sidesteps sqlite3's "single connection per thread" rules
entirely, which matters because the gateway's LoRa receive callback and its
main processing loop run on different threads.
============================================================================
"""

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator, List, Optional

# VTD_DB_PATH lets the hardware-free simulation (simulate.py) use its own
# database file, so running it can never touch the real gateway's data.
DEFAULT_DB_PATH = os.environ.get("VTD_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "vehicle_theft_detection.db"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id             INTEGER NOT NULL,
    latitude            REAL NOT NULL,
    longitude           REAL NOT NULL,
    speed_kmph          REAL NOT NULL,
    heading_deg         REAL NOT NULL,
    satellites          INTEGER NOT NULL,
    motion_flag         INTEGER NOT NULL,
    gps_valid           INTEGER NOT NULL,
    battery_millivolts  INTEGER NOT NULL,
    sequence_number     INTEGER,
    packet_type         INTEGER,
    rssi_dbm            INTEGER NOT NULL,
    snr_db              REAL NOT NULL,
    anomaly_score       REAL,
    is_anomaly          INTEGER NOT NULL DEFAULT 0,
    received_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_node_time ON telemetry(node_id, received_at);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id      INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    score        REAL,
    latitude     REAL NOT NULL,
    longitude    REAL NOT NULL,
    speed_kmph   REAL NOT NULL,
    sms_sent     INTEGER NOT NULL DEFAULT 0,
    sent_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_node_time ON alerts(node_id, sent_at);

-- Single-row heartbeat the gateway rewrites every few seconds, so the
-- dashboard can show whether the gateway process is alive and whether the
-- SIM800C can actually send SMS — things it can't infer from telemetry.
CREATE TABLE IF NOT EXISTS gateway_status (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    started_at              REAL NOT NULL,
    updated_at              REAL NOT NULL,
    gsm_ready               INTEGER NOT NULL,
    gsm_port                TEXT,
    alert_numbers           INTEGER NOT NULL,
    pending_alerts          INTEGER NOT NULL,
    lora_packets            INTEGER NOT NULL,
    lora_crc_errors         INTEGER NOT NULL,
    loss_of_contact_alerts  INTEGER NOT NULL
);
"""


@contextmanager
def _connect(db_path: str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        # WAL lets the dashboard read while the gateway writes, without
        # either blocking the other. Some filesystems (network mounts,
        # certain container overlays) don't support the shared-memory
        # locking WAL needs — fall back to the default journal mode rather
        # than crashing the whole gateway over it.
        conn.execute("PRAGMA journal_mode=WAL;")
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a database created by an older version of this module up to the
    current schema, without losing any rows.

    v1 -> v2: the ESP32 no longer sends `uptime_seconds` (it reset on every
    deep-sleep wake, so it carried no information). Its slot in the packet
    now holds a per-node sequence number, used for duplicate/replay
    detection and Packet Delivery Ratio. Old databases have a NOT NULL
    `uptime_seconds` column and no `sequence_number`/`packet_type` columns,
    so the table is rebuilt with the new columns and old rows are copied
    across (their sequence_number is left NULL — old uptime values are NOT
    sequence numbers and must not be mixed into PDR calculations)."""
    def _needs_migration() -> bool:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(telemetry)").fetchall()}
        return "uptime_seconds" in columns

    if not _needs_migration():
        return

    # The gateway and the dashboard both call init_db(); take the write lock
    # first and re-check, so if both start at once only one of them migrates.
    conn.commit()
    conn.isolation_level = None          # manual transaction control
    conn.execute("BEGIN IMMEDIATE")
    if not _needs_migration():
        conn.execute("COMMIT")
        conn.isolation_level = ""
        return

    # Individual execute() calls, not executescript(): executescript() issues
    # its own COMMIT first, which would release the BEGIN IMMEDIATE lock.
    statements = [
        "ALTER TABLE telemetry RENAME TO telemetry_v1",
        """CREATE TABLE telemetry (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id             INTEGER NOT NULL,
            latitude            REAL NOT NULL,
            longitude           REAL NOT NULL,
            speed_kmph          REAL NOT NULL,
            heading_deg         REAL NOT NULL,
            satellites          INTEGER NOT NULL,
            motion_flag         INTEGER NOT NULL,
            gps_valid           INTEGER NOT NULL,
            battery_millivolts  INTEGER NOT NULL,
            sequence_number     INTEGER,
            packet_type         INTEGER,
            rssi_dbm            INTEGER NOT NULL,
            snr_db              REAL NOT NULL,
            anomaly_score       REAL,
            is_anomaly          INTEGER NOT NULL DEFAULT 0,
            received_at         REAL NOT NULL
        )""",
        """INSERT INTO telemetry
            (id, node_id, latitude, longitude, speed_kmph, heading_deg, satellites,
             motion_flag, gps_valid, battery_millivolts, sequence_number, packet_type,
             rssi_dbm, snr_db, anomaly_score, is_anomaly, received_at)
        SELECT id, node_id, latitude, longitude, speed_kmph, heading_deg, satellites,
               motion_flag, gps_valid, battery_millivolts, NULL, NULL,
               rssi_dbm, snr_db, anomaly_score, is_anomaly, received_at
        FROM telemetry_v1""",
        "DROP TABLE telemetry_v1",
        "CREATE INDEX IF NOT EXISTS idx_telemetry_node_time ON telemetry(node_id, received_at)",
    ]
    try:
        for sql in statements:
            conn.execute(sql)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = ""


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


def insert_telemetry(record: dict, db_path: str = DEFAULT_DB_PATH) -> int:
    """record keys: node_id, latitude, longitude, speed_kmph, heading_deg,
    satellites, motion_flag, gps_valid, battery_millivolts, sequence_number
    (optional), packet_type (optional), rssi_dbm, snr_db, anomaly_score
    (optional), is_anomaly (optional), received_at (optional, defaults to now)."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO telemetry
               (node_id, latitude, longitude, speed_kmph, heading_deg, satellites,
                motion_flag, gps_valid, battery_millivolts, sequence_number, packet_type,
                rssi_dbm, snr_db, anomaly_score, is_anomaly, received_at)
               VALUES (:node_id, :latitude, :longitude, :speed_kmph, :heading_deg,
                       :satellites, :motion_flag, :gps_valid, :battery_millivolts,
                       :sequence_number, :packet_type, :rssi_dbm, :snr_db, :anomaly_score,
                       :is_anomaly, :received_at)""",
            {
                "node_id": record["node_id"],
                "latitude": record["latitude"],
                "longitude": record["longitude"],
                "speed_kmph": record["speed_kmph"],
                "heading_deg": record["heading_deg"],
                "satellites": record["satellites"],
                "motion_flag": int(bool(record["motion_flag"])),
                "gps_valid": int(bool(record["gps_valid"])),
                "battery_millivolts": record["battery_millivolts"],
                "sequence_number": record.get("sequence_number"),
                "packet_type": record.get("packet_type"),
                "rssi_dbm": record["rssi_dbm"],
                "snr_db": record["snr_db"],
                "anomaly_score": record.get("anomaly_score"),
                "is_anomaly": int(bool(record.get("is_anomaly", False))),
                "received_at": record.get("received_at", time.time()),
            },
        )
        return cur.lastrowid


def insert_alert(record: dict, db_path: str = DEFAULT_DB_PATH) -> int:
    """record keys: node_id, reason, score, latitude, longitude, speed_kmph,
    sms_sent (optional), sent_at (optional, defaults to now)."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO alerts
               (node_id, reason, score, latitude, longitude, speed_kmph, sms_sent, sent_at)
               VALUES (:node_id, :reason, :score, :latitude, :longitude, :speed_kmph, :sms_sent, :sent_at)""",
            {
                "node_id": record["node_id"],
                "reason": record["reason"],
                "score": record.get("score"),
                "latitude": record["latitude"],
                "longitude": record["longitude"],
                "speed_kmph": record["speed_kmph"],
                "sms_sent": int(bool(record.get("sms_sent", False))),
                "sent_at": record.get("sent_at", time.time()),
            },
        )
        return cur.lastrowid


def mark_alert_sms_sent(alert_id: int, db_path: str = DEFAULT_DB_PATH) -> None:
    """Flip sms_sent to 1 once a queued/retried alert SMS is actually
    delivered to the module (the alert row is written the moment the alert
    is raised, so it is never lost even if the SMS takes minutes to go out)."""
    with _connect(db_path) as conn:
        conn.execute("UPDATE alerts SET sms_sent = 1 WHERE id = ?", (alert_id,))


def write_gateway_status(status: dict, db_path: str = DEFAULT_DB_PATH) -> None:
    """Upsert the gateway's single heartbeat row (see the gateway_status table)."""
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO gateway_status
               (id, started_at, updated_at, gsm_ready, gsm_port, alert_numbers, pending_alerts,
                lora_packets, lora_crc_errors, loss_of_contact_alerts)
               VALUES (1, :started_at, :updated_at, :gsm_ready, :gsm_port, :alert_numbers,
                       :pending_alerts, :lora_packets, :lora_crc_errors, :loss_of_contact_alerts)
               ON CONFLICT(id) DO UPDATE SET
                 started_at=excluded.started_at, updated_at=excluded.updated_at,
                 gsm_ready=excluded.gsm_ready, gsm_port=excluded.gsm_port,
                 alert_numbers=excluded.alert_numbers, pending_alerts=excluded.pending_alerts,
                 lora_packets=excluded.lora_packets, lora_crc_errors=excluded.lora_crc_errors,
                 loss_of_contact_alerts=excluded.loss_of_contact_alerts""",
            {**status, "gsm_ready": int(bool(status["gsm_ready"])),
             "loss_of_contact_alerts": int(bool(status["loss_of_contact_alerts"]))},
        )


def get_gateway_status(db_path: str = DEFAULT_DB_PATH) -> Optional[sqlite3.Row]:
    with _connect(db_path) as conn:
        return conn.execute("SELECT * FROM gateway_status WHERE id = 1").fetchone()


def get_last_valid_fix(node_id: int, db_path: str = DEFAULT_DB_PATH) -> Optional[sqlite3.Row]:
    """Most recent row with a fresh GPS fix — the vehicle's last known position."""
    with _connect(db_path) as conn:
        return conn.execute(
            """SELECT latitude, longitude, received_at FROM telemetry
               WHERE node_id = ? AND gps_valid = 1 ORDER BY id DESC LIMIT 1""",
            (node_id,),
        ).fetchone()


def get_latest_threat_alert(node_id: int, since_epoch: float, exclude_contact: bool = False,
                            db_path: str = DEFAULT_DB_PATH) -> Optional[sqlite3.Row]:
    """Most recent alert for a vehicle since `since_epoch`, ignoring follow-up
    location updates (those are not new threats). With exclude_contact=True,
    loss-of-contact alerts are ignored too, leaving only theft alerts."""
    excluded = ("location_update", "loss_of_contact") if exclude_contact else ("location_update",)
    placeholders = ",".join("?" for _ in excluded)
    with _connect(db_path) as conn:
        return conn.execute(
            f"""SELECT id, reason, sent_at FROM alerts
               WHERE node_id = ? AND sent_at >= ? AND reason NOT IN ({placeholders})
               ORDER BY id DESC LIMIT 1""",
            (node_id, since_epoch, *excluded),
        ).fetchone()


def get_alerts_by_ids(ids: List[int], db_path: str = DEFAULT_DB_PATH) -> List[sqlite3.Row]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with _connect(db_path) as conn:
        return conn.execute(f"SELECT * FROM alerts WHERE id IN ({placeholders})", list(ids)).fetchall()


def get_recent_telemetry_per_node(limit_per_node: int, db_path: str = DEFAULT_DB_PATH) -> List[sqlite3.Row]:
    """The most recent `limit_per_node` rows for every node, oldest first.
    Used by the gateway at startup to rebuild each vehicle's anomaly-model
    history, so a gateway restart doesn't drop every vehicle back into
    cold-start mode."""
    with _connect(db_path) as conn:
        return conn.execute(
            """SELECT * FROM (
                   SELECT t.*, ROW_NUMBER() OVER (PARTITION BY node_id ORDER BY id DESC) AS rn
                   FROM telemetry t
               ) WHERE rn <= ? ORDER BY id ASC""",
            (limit_per_node,),
        ).fetchall()


# ---------------------------------------------------------------------------
# Read helpers (used by the dashboard backend)
# ---------------------------------------------------------------------------
def get_known_node_ids(db_path: str = DEFAULT_DB_PATH) -> List[int]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT DISTINCT node_id FROM telemetry ORDER BY node_id").fetchall()
        return [r["node_id"] for r in rows]


def get_latest_telemetry(node_id: Optional[int] = None, db_path: str = DEFAULT_DB_PATH) -> List[sqlite3.Row]:
    """Latest row per node (or just for one node if node_id is given)."""
    with _connect(db_path) as conn:
        if node_id is not None:
            rows = conn.execute(
                "SELECT * FROM telemetry WHERE node_id = ? ORDER BY id DESC LIMIT 1", (node_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT t.* FROM telemetry t
                   INNER JOIN (SELECT node_id, MAX(id) AS max_id FROM telemetry GROUP BY node_id) latest
                   ON t.node_id = latest.node_id AND t.id = latest.max_id"""
            ).fetchall()
        return rows


def get_telemetry_since(node_id: int, since_epoch: float, limit: int = 5000,
                         db_path: str = DEFAULT_DB_PATH) -> List[sqlite3.Row]:
    with _connect(db_path) as conn:
        return conn.execute(
            """SELECT * FROM telemetry WHERE node_id = ? AND received_at >= ?
               ORDER BY received_at ASC LIMIT ?""",
            (node_id, since_epoch, limit),
        ).fetchall()


def get_telemetry_after_id(last_id: int, db_path: str = DEFAULT_DB_PATH) -> List[sqlite3.Row]:
    """Used by the dashboard's polling loop to find rows newer than the last
    one it has already broadcast to WebSocket clients."""
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM telemetry WHERE id > ? ORDER BY id ASC", (last_id,)
        ).fetchall()


def get_alerts_after_id(last_id: int, db_path: str = DEFAULT_DB_PATH) -> List[sqlite3.Row]:
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM alerts WHERE id > ? ORDER BY id ASC", (last_id,)
        ).fetchall()


def get_recent_alerts(node_id: Optional[int] = None, limit: int = 100,
                       db_path: str = DEFAULT_DB_PATH) -> List[sqlite3.Row]:
    with _connect(db_path) as conn:
        if node_id is not None:
            return conn.execute(
                "SELECT * FROM alerts WHERE node_id = ? ORDER BY id DESC LIMIT ?", (node_id, limit)
            ).fetchall()
        return conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def get_max_ids(db_path: str = DEFAULT_DB_PATH) -> dict:
    """Starting cursor positions for the dashboard's change-polling loop."""
    with _connect(db_path) as conn:
        t = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM telemetry").fetchone()["m"]
        a = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM alerts").fetchone()["m"]
        return {"telemetry": t, "alerts": a}


def get_counts(db_path: str = DEFAULT_DB_PATH) -> dict:
    """Total row counts across all vehicles — used by the dashboard's System
    Status page. Cheap even at fairly large row counts since both are simple
    COUNT(*) queries against an indexed table."""
    with _connect(db_path) as conn:
        t = conn.execute("SELECT COUNT(*) AS c FROM telemetry").fetchone()["c"]
        a = conn.execute("SELECT COUNT(*) AS c FROM alerts").fetchone()["c"]
        return {"telemetry": t, "alerts": a}
