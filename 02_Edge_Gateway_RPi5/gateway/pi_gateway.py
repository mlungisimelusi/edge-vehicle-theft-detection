#!/usr/bin/env python3
"""
============================================================================
 Edge-Based Vehicle Theft Detection System — LoRa-GSM Hybrid Network
 EDGE GATEWAY (Raspberry Pi 5, Ubuntu Linux)
============================================================================
Continuously listens for LoRa telemetry from the ESP32 mobile node(s),
scores each packet with per-vehicle Isolation Forest anomaly models, and
sends SIM800C SMS alerts when an anomaly is confirmed or when a vehicle
stops reporting altogether.

Threads:
    gpiozero interrupt thread  SX1278 DIO0 -> raw bytes put on a queue
    main thread                parse -> sequence check -> anomaly scoring ->
                               SQLite -> alert decisions; also runs the
                               loss-of-contact watchdog and the LoRa radio
                               health check
    alert worker thread        owns the SIM800C exclusively: sends SMS,
                               retries unsent alerts, reconnects the modem
                               if it drops out — so a slow or failing SMS
                               send never blocks packet processing

Alert types:
    anomaly          Isolation Forest (or the cold-start motion rule) flags
                     a packet as abnormal.
    loss of contact  a vehicle that was reporting goes silent for longer
                     than CONTACT_TIMEOUT (default 16 min = 3 missed 5-min
                     heartbeats + margin). This is what catches a vehicle
                     driven out of LoRa range, a jammed 433 MHz channel, or a
                     destroyed/disconnected node — the cases where no
                     anomalous packet ever arrives.
    location update  one follow-up SMS with a fresh GPS fix, sent when an
                     alert went out with only a stale position.

Telemetry packet formats (little-endian, packed — must match the ESP32
firmware's TelemetryPacket struct byte for byte):
    v2 (current), 31 bytes, "<BBIIffffBBBH":
        uint8  protocolVersion (=2), uint8 packetType, uint32 nodeId,
        uint32 sequence, float lat, float lon, float speedKmph,
        float headingDeg, uint8 satellites, uint8 motionFlag,
        uint8 gpsValid, uint16 batteryMilliVolts
    v1 (legacy firmware), 29 bytes, "<IffffBBBHI":
        accepted for backward compatibility (no sequence number / type).

Configuration: gateway_config.json next to this file (see
gateway_config.example.json). The alert phone number(s) can also be given
with the VTD_ALERT_NUMBERS environment variable (comma-separated).

Run:   python3 pi_gateway.py
Stop:  Ctrl+C (or SIGTERM from systemd) — radio, modem and threads are
       shut down cleanly.
============================================================================
"""

import json
import logging
import math
import os
import queue
import re
import signal
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
from sklearn.ensemble import IsolationForest

import telemetry_store
from sim800_gsm import SMS_MAX_CHARS, Sim800Gsm
from sx127x_lora import Sx127xLoRa

# ---------------------------------------------------------------------------
# Hardware configuration (fixed by the wiring in Phase1_Hardware_Wiring.md)
# ---------------------------------------------------------------------------
LORA_SPI_BUS = 0
LORA_SPI_DEVICE = 0
LORA_RESET_PIN = 25   # BCM GPIO25 / physical pin 22
LORA_DIO0_PIN = 24    # BCM GPIO24 / physical pin 18

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway_config.json")

# ---------------------------------------------------------------------------
# Packet formats
# ---------------------------------------------------------------------------
TELEMETRY_V2_FORMAT = "<BBIIffffBBBH"
TELEMETRY_V2_SIZE = struct.calcsize(TELEMETRY_V2_FORMAT)     # 31
TELEMETRY_V1_FORMAT = "<IffffBBBHI"
TELEMETRY_V1_SIZE = struct.calcsize(TELEMETRY_V1_FORMAT)     # 29
PROTOCOL_VERSION = 2

PACKET_TYPE_HEARTBEAT = 0
PACKET_TYPE_MOTION_ALERT = 1     # sent immediately on a motion wake, before GPS
PACKET_TYPE_MOTION_UPDATE = 2    # sent after the GPS attempt on a motion wake
PACKET_TYPE_NAMES = {
    PACKET_TYPE_HEARTBEAT: "heartbeat",
    PACKET_TYPE_MOTION_ALERT: "motion",
    PACKET_TYPE_MOTION_UPDATE: "motion+gps",
}

# A sequence number that drops back to this value or below is treated as a
# node restart (RTC memory is cleared on power loss, so the ESP32 restarts
# counting from 1).
SEQUENCE_RESET_THRESHOLD = 5

# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------
ALERT_COOLDOWN_SECONDS = 300          # max one anomaly SMS per vehicle per 5 min
HEARTBEAT_INTERVAL_SECONDS = 300      # must match the ESP32's HEARTBEAT_INTERVAL_US
DEFAULT_CONTACT_TIMEOUT_MINUTES = 16  # 3 missed heartbeats + 1 min margin
LOCATION_FOLLOWUP_WINDOW_SECONDS = 600
UNSENT_ALERT_MAX_AGE_SECONDS = 1800   # keep retrying an unsent SMS for 30 min
GSM_RECONNECT_INTERVAL_SECONDS = 30
GSM_HEALTH_CHECK_INTERVAL_SECONDS = 300
WATCHDOG_INTERVAL_SECONDS = 10
RADIO_HEALTH_CHECK_INTERVAL_SECONDS = 60

# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------
# With a 5-minute heartbeat a parked car produces ~288 packets/day, so a
# window of 2000 samples holds roughly a week of history — enough for the
# hour-of-day features to mean something.
FEATURE_WINDOW_SIZE = 2000
# Learning period before each model is trusted. During it, any hardware
# motion wake raises an alert (cold-start rule). It must cover real usage:
# with only a handful of samples (the old value was 20 — under 2 hours of
# heartbeats), the model learned a few bench shake tests as "normal" and
# then stayed silent when the node was shaken again.
#   context model   288 packets = about one day of 5-minute heartbeats
#   movement model  100 fresh GPS fixes
CONTEXT_MIN_SAMPLES = 288
MOVEMENT_MIN_SAMPLES = 100
REFIT_INTERVAL_SAMPLES = 5
ISOLATION_FOREST_CONTAMINATION = 0.05
ISOLATION_FOREST_N_ESTIMATORS = 100
RANDOM_STATE = 42
STATIONARY_SPEED_KMPH = 3.0          # below this, GPS heading is just noise
MAX_IMPLIED_SPEED_KMPH = 300.0
MAX_IMPLIED_SPEED_GAP_SECONDS = 1800

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pi_gateway")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
E164_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")


@dataclass
class GatewayConfig:
    alert_phone_numbers: List[str]
    gsm_serial_port: Optional[str]
    contact_timeout_seconds: float
    loss_of_contact_alerts: bool
    known_node_ids: List[int]


def load_config(path: str = CONFIG_PATH) -> GatewayConfig:
    raw: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            logger.info("Loaded configuration from %s", path)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Could not read %s (%s) — using defaults.", path, exc)
    else:
        logger.warning("No %s found — using defaults. Copy gateway_config.example.json to "
                       "gateway_config.json and put your alert number(s) in it.",
                       os.path.basename(path))

    numbers = raw.get("alert_phone_numbers", [])
    env_numbers = os.environ.get("VTD_ALERT_NUMBERS")
    if env_numbers:
        numbers = [n.strip() for n in env_numbers.split(",") if n.strip()]

    valid_numbers = []
    for number in numbers:
        number = str(number).replace(" ", "")
        if not E164_PATTERN.match(number):
            logger.error("Ignoring alert number %r — use international format, e.g. +27821234567.",
                         number)
            continue
        if set(number[3:]) == {"0"}:
            logger.error("Ignoring alert number %r — it looks like a placeholder.", number)
            continue
        valid_numbers.append(number)

    return GatewayConfig(
        alert_phone_numbers=valid_numbers,
        gsm_serial_port=raw.get("gsm_serial_port") or None,
        contact_timeout_seconds=float(raw.get("contact_timeout_minutes",
                                              DEFAULT_CONTACT_TIMEOUT_MINUTES)) * 60.0,
        loss_of_contact_alerts=bool(raw.get("loss_of_contact_alerts", True)),
        known_node_ids=[int(n) for n in raw.get("known_node_ids", [])],
    )


# ---------------------------------------------------------------------------
# Telemetry parsing
# ---------------------------------------------------------------------------
@dataclass
class Telemetry:
    node_id: int
    latitude: float
    longitude: float
    speed_kmph: float
    heading_deg: float
    satellites: int
    motion_flag: bool
    gps_valid: bool
    battery_millivolts: int
    rssi_dbm: int
    snr_db: float
    sequence: Optional[int] = None
    packet_type: Optional[int] = None
    protocol_version: int = PROTOCOL_VERSION
    received_at: float = field(default_factory=time.time)


def parse_telemetry_packet(payload: bytes, rssi_dbm: int, snr_db: float) -> Optional[Telemetry]:
    try:
        if len(payload) == TELEMETRY_V2_SIZE:
            (version, packet_type, node_id, sequence, lat, lon, speed, heading, sats,
             motion_flag, gps_valid, batt_mv) = struct.unpack(TELEMETRY_V2_FORMAT, payload)
            if version != PROTOCOL_VERSION:
                logger.warning("Dropping %d-byte packet with unknown protocol version %d.",
                               len(payload), version)
                return None
            if packet_type not in PACKET_TYPE_NAMES:
                logger.warning("Dropping packet with unknown packet type %d.", packet_type)
                return None
        elif len(payload) == TELEMETRY_V1_SIZE:
            (node_id, lat, lon, speed, heading, sats, motion_flag, gps_valid,
             batt_mv, _uptime) = struct.unpack(TELEMETRY_V1_FORMAT, payload)
            version, packet_type, sequence = 1, None, None
        else:
            logger.warning("Dropping packet with unexpected size %d bytes (expected %d, or %d for "
                           "legacy v1 firmware).", len(payload), TELEMETRY_V2_SIZE, TELEMETRY_V1_SIZE)
            return None
    except struct.error as exc:
        logger.warning("Failed to unpack telemetry packet: %s", exc)
        return None

    # Sanity checks. CRC protects against corruption in the air, but not
    # against a different LoRa device on the same frequency/sync word.
    if not all(math.isfinite(v) for v in (lat, lon, speed, heading)):
        logger.warning("Dropping packet from node %d with non-finite values.", node_id)
        return None
    gps_valid = bool(gps_valid)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        logger.warning("Node %d sent out-of-range coordinates (%.5f, %.5f) — marking GPS invalid.",
                       node_id, lat, lon)
        gps_valid = False
    if gps_valid and lat == 0.0 and lon == 0.0:
        gps_valid = False

    return Telemetry(
        node_id=node_id, latitude=lat, longitude=lon,
        speed_kmph=max(0.0, speed), heading_deg=heading % 360.0,
        satellites=sats, motion_flag=bool(motion_flag), gps_valid=gps_valid,
        battery_millivolts=batt_mv, rssi_dbm=rssi_dbm, snr_db=snr_db,
        sequence=sequence, packet_type=packet_type, protocol_version=version,
    )


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------
def _cyclic(value: float, period: float) -> Tuple[float, float]:
    """Encode a circular quantity (hour of day, compass heading) as a point on
    the unit circle, so 23:00 is next to 00:00 and 359 deg is next to 1 deg.
    A raw number would put them at opposite ends of the feature range."""
    angle = 2.0 * math.pi * (value % period) / period
    return math.sin(angle), math.cos(angle)


def _hour_of_day(epoch: float) -> float:
    dt = datetime.fromtimestamp(epoch)
    return dt.hour + dt.minute / 60.0


def context_features(t: Telemetry) -> np.ndarray:
    """Features available for EVERY packet, including ones without a fresh
    GPS fix: was the vehicle moving, and at what time of day."""
    hour_sin, hour_cos = _cyclic(_hour_of_day(t.received_at), 24.0)
    return np.array([1.0 if t.motion_flag else 0.0, hour_sin, hour_cos], dtype=float)


def movement_features(t: Telemetry, previous_fix: Optional[Telemetry]) -> np.ndarray:
    """Full feature vector — only meaningful when this packet has a fresh
    GPS fix, so it is never built for stale-GPS packets (their speed and
    heading are zeros, which would teach the model a false 'normal')."""
    if t.speed_kmph >= STATIONARY_SPEED_KMPH:
        heading_sin, heading_cos = _cyclic(t.heading_deg, 360.0)
    else:
        heading_sin, heading_cos = 0.0, 0.0

    # Speed implied by the distance between consecutive fixes. Disagreement
    # with the GPS-reported speed (or an impossible value) points at a
    # position jump — towing on a truck, or a spoofed GPS.
    implied_speed = t.speed_kmph
    if previous_fix is not None:
        dt_s = t.received_at - previous_fix.received_at
        if 1.0 < dt_s < MAX_IMPLIED_SPEED_GAP_SECONDS:
            km = haversine_km(previous_fix.latitude, previous_fix.longitude, t.latitude, t.longitude)
            implied_speed = min(MAX_IMPLIED_SPEED_KMPH, km / (dt_s / 3600.0))

    hour_sin, hour_cos = _cyclic(_hour_of_day(t.received_at), 24.0)
    return np.array([
        t.speed_kmph, heading_sin, heading_cos, implied_speed,
        1.0 if t.motion_flag else 0.0, hour_sin, hour_cos,
    ], dtype=float)


# ---------------------------------------------------------------------------
# Anomaly models
# ---------------------------------------------------------------------------
class RollingIsolationForest:
    """An Isolation Forest over a rolling window of one vehicle's own
    history. It is refit on the history BEFORE the newest sample is added,
    so a sample is never used to help judge itself."""

    def __init__(self, name: str, min_samples: int):
        self.name = name
        self.min_samples = min_samples
        self.history: deque = deque(maxlen=FEATURE_WINDOW_SIZE)
        self.model: Optional[IsolationForest] = None
        self.samples_since_fit = 0

    @property
    def ready(self) -> bool:
        return self.model is not None

    def fit_if_due(self) -> None:
        if len(self.history) < self.min_samples:
            return
        if self.model is not None and self.samples_since_fit < REFIT_INTERVAL_SAMPLES:
            return
        model = IsolationForest(
            n_estimators=ISOLATION_FOREST_N_ESTIMATORS,
            contamination=ISOLATION_FOREST_CONTAMINATION,
            random_state=RANDOM_STATE,
        )
        model.fit(np.vstack(self.history))
        self.model = model
        self.samples_since_fit = 0

    def score(self, x: np.ndarray) -> Tuple[bool, float]:
        sample = x.reshape(1, -1)
        is_outlier = self.model.predict(sample)[0] == -1
        return bool(is_outlier), float(self.model.decision_function(sample)[0])

    def add(self, x: np.ndarray) -> None:
        self.history.append(x)
        self.samples_since_fit += 1


class NodeAnomalyDetector:
    """Two models per vehicle:
        movement model  speed, heading, implied speed, motion, time of day —
                        used for packets WITH a fresh GPS fix.
        context model   motion + time of day only — used for packets without
                        a fresh fix (e.g. the immediate motion packet sent
                        before the GPS has locked), which can't be judged on
                        movement features they don't have.
    Until either model has enough history, a hardware motion wake is treated
    as an anomaly (cold-start rule), so the system is never blind while it
    is still learning."""

    def __init__(self, node_id: int):
        self.node_id = node_id
        self.movement_model = RollingIsolationForest("movement", MOVEMENT_MIN_SAMPLES)
        self.context_model = RollingIsolationForest("context", CONTEXT_MIN_SAMPLES)

    def evaluate(self, t: Telemetry, previous_fix: Optional[Telemetry]) -> Dict:
        self.movement_model.fit_if_due()
        self.context_model.fit_if_due()

        result = {"is_anomaly": False, "reason": "baseline_insufficient", "score": None}
        ctx = context_features(t)
        mov = movement_features(t, previous_fix) if t.gps_valid else None

        if mov is not None and self.movement_model.ready:
            is_outlier, score = self.movement_model.score(mov)
            result.update(score=score, reason="isolation_forest" if is_outlier else "normal",
                          is_anomaly=is_outlier)
        elif self.context_model.ready:
            is_outlier, score = self.context_model.score(ctx)
            result.update(score=score, reason="isolation_forest_context" if is_outlier else "normal",
                          is_anomaly=is_outlier)
        elif t.motion_flag:
            result.update(is_anomaly=True, reason="coldstart_hardware_motion_flag")

        self.learn(t, previous_fix, ctx, mov)
        return result

    def learn(self, t: Telemetry, previous_fix: Optional[Telemetry],
              ctx: Optional[np.ndarray] = None, mov: Optional[np.ndarray] = None) -> None:
        self.context_model.add(ctx if ctx is not None else context_features(t))
        if t.gps_valid:
            self.movement_model.add(mov if mov is not None else movement_features(t, previous_fix))


@dataclass
class NodeState:
    node_id: int
    detector: NodeAnomalyDetector
    last_seen: float = 0.0
    last_valid_fix: Optional[Telemetry] = None
    last_packet: Optional[Telemetry] = None
    last_sequence: Optional[int] = None
    reset_candidate_sequence: Optional[int] = None
    last_alert_time: float = 0.0
    contact_lost: bool = False
    location_followup_until: float = 0.0
    warned_legacy_firmware: bool = False
    packets_missed: int = 0

    def cooldown_active(self, now: float) -> bool:
        return (now - self.last_alert_time) < ALERT_COOLDOWN_SECONDS


# ---------------------------------------------------------------------------
# SMS formatting — every message must fit in one 160-character SMS
# ---------------------------------------------------------------------------
REASON_LABELS = {
    "coldstart_hardware_motion_flag": "Motion detected (learning)",
    "isolation_forest": "Unusual movement pattern (ML)",
    "isolation_forest_context": "Motion at unusual time (ML)",
    "loss_of_contact": "Contact lost",
    "location_update": "Location update",
}


def _fmt_time(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch)
    if dt.date() == datetime.now().date():
        return dt.strftime("%H:%M")
    return dt.strftime("%d %b %H:%M")


def _maps_link(fix: Telemetry) -> str:
    return f"https://maps.google.com/?q={fix.latitude:.6f},{fix.longitude:.6f}"


def _fit_sms(lines: List[str]) -> str:
    message = "\n".join(line for line in lines if line)
    if len(message) > SMS_MAX_CHARS:
        logger.warning("SMS text is %d chars — trimming to %d.", len(message), SMS_MAX_CHARS)
        message = message[:SMS_MAX_CHARS]
    return message


def _battery_text(t: Telemetry) -> str:
    # 0 mV means the node isn't measuring its battery (no divider wired).
    return f" Batt {t.battery_millivolts / 1000:.2f}V" if t.battery_millivolts > 0 else ""


def format_anomaly_sms(t: Telemetry, reason: str, last_fix: Optional[Telemetry]) -> str:
    lines = [f"THEFT ALERT vehicle {t.node_id}", REASON_LABELS.get(reason, reason)[:40]]
    if t.gps_valid:
        lines.append(f"GPS fix {_fmt_time(t.received_at)} (live)")
        lines.append(_maps_link(t))
        lines.append(f"Speed {t.speed_kmph:.0f}km/h{_battery_text(t)}")
    else:
        if last_fix is not None:
            lines.append(f"Last fix {_fmt_time(last_fix.received_at)} (stale)")
            lines.append(_maps_link(last_fix))
        else:
            lines.append("No GPS fix yet")
        lines.append(f"RSSI {t.rssi_dbm}dBm{_battery_text(t)}")
    return _fit_sms(lines)


def _fmt_duration(seconds: float) -> str:
    return f"{seconds:.0f} s" if seconds < 60 else f"{seconds / 60.0:.0f} min"


def format_loss_of_contact_sms(node_id: int, silent_minutes: float, last_fix: Optional[Telemetry]) -> str:
    lines = [f"CONTACT LOST vehicle {node_id}",
             f"No signal for {_fmt_duration(silent_minutes * 60.0)} - out of range or jammed?"]
    if last_fix is not None:
        lines.append(f"Last fix {_fmt_time(last_fix.received_at)}")
        lines.append(_maps_link(last_fix))
    else:
        lines.append("No GPS fix on record")
    return _fit_sms(lines)


def format_location_update_sms(t: Telemetry) -> str:
    return _fit_sms([
        f"LOCATION UPDATE vehicle {t.node_id}",
        f"GPS fix {_fmt_time(t.received_at)}, {t.speed_kmph:.0f}km/h",
        _maps_link(t),
    ])


# ---------------------------------------------------------------------------
# Alert worker — the only thread that touches the SIM800C after startup
# ---------------------------------------------------------------------------
@dataclass
class AlertJob:
    alert_id: int
    node_id: int
    message: str
    created_at: float = field(default_factory=time.time)
    delivered_to: set = field(default_factory=set)


class AlertDispatcher(threading.Thread):
    def __init__(self, serial_port: Optional[str], phone_numbers: List[str]):
        super().__init__(name="alert-dispatcher", daemon=True)
        self._gsm = Sim800Gsm(port=serial_port)
        self._phone_numbers = phone_numbers
        self._jobs: "queue.Queue[AlertJob]" = queue.Queue()
        self._pending: List[AlertJob] = []
        self._stop_event = threading.Event()
        self._ready = False
        self._last_reconnect_attempt = 0.0
        self._last_health_check = time.monotonic()

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def pending_count(self) -> int:
        """Alerts recorded but not yet delivered by SMS (approximate — read
        without a lock, used only for the dashboard's status display)."""
        return len(self._pending) + self._jobs.qsize()

    @property
    def gsm_port(self) -> Optional[str]:
        return self._gsm.port_name

    # --- called from the main thread ---------------------------------
    def connect(self) -> bool:
        """Initial modem bring-up, run once before the thread starts."""
        self._ready = self._bring_up()
        self._last_reconnect_attempt = time.monotonic()
        if not self._phone_numbers:
            logger.error("No valid alert phone numbers configured — alerts will be recorded on the "
                         "dashboard but no SMS can be sent. Add them to gateway_config.json.")
        return self._ready

    def submit(self, node_id: int, reason: str, score: Optional[float], fix: Optional[Telemetry],
               speed_kmph: float, message: str) -> None:
        """Record the alert in the database immediately (so it's on the
        dashboard even if the SMS takes minutes), then queue the SMS."""
        alert_id = telemetry_store.insert_alert({
            "node_id": node_id, "reason": reason, "score": score,
            "latitude": fix.latitude if fix else 0.0,
            "longitude": fix.longitude if fix else 0.0,
            "speed_kmph": speed_kmph, "sms_sent": False, "sent_at": time.time(),
        })
        logger.warning("ALERT #%d for node %d (%s) queued for SMS:\n%s",
                       alert_id, node_id, reason, message)
        self._jobs.put(AlertJob(alert_id=alert_id, node_id=node_id, message=message))

    def stop(self) -> None:
        self._stop_event.set()

    # --- worker thread -------------------------------------------------
    def _bring_up(self) -> bool:
        if not self._gsm.open():
            logger.error("SIM800C not responding — alerts will be queued and retried every %ds.",
                         GSM_RECONNECT_INTERVAL_SECONDS)
            return False
        if not self._gsm.sim_ready():
            self._gsm.close()
            return False
        if not self._gsm.is_network_registered(timeout_s=20.0):
            logger.error("SIM800C is not registered on the network — alerts will be queued and "
                         "retried. Check antenna, SIM credit and 2G coverage.")
            self._gsm.close()
            return False
        logger.info("SIM800C ready on %s, network registered, signal ~%s dBm.",
                    self._gsm.port_name, self._gsm.signal_quality())
        return True

    def _try_reconnect(self) -> None:
        now = time.monotonic()
        if now - self._last_reconnect_attempt < GSM_RECONNECT_INTERVAL_SECONDS:
            return
        self._last_reconnect_attempt = now
        logger.info("Trying to reconnect to the SIM800C (%d alert(s) waiting)...", len(self._pending))
        self._gsm.close()
        self._ready = self._bring_up()

    def _deliver_pending(self) -> None:
        now = time.time()
        still_pending: List[AlertJob] = []
        for index, job in enumerate(self._pending):
            if now - job.created_at > UNSENT_ALERT_MAX_AGE_SECONDS:
                logger.error("Giving up on alert #%d for node %d — not delivered within %d min.",
                             job.alert_id, job.node_id, UNSENT_ALERT_MAX_AGE_SECONDS // 60)
                continue
            for number in self._phone_numbers:
                if number in job.delivered_to:
                    continue
                if self._gsm.send_sms(number, job.message):
                    if not job.delivered_to:
                        telemetry_store.mark_alert_sms_sent(job.alert_id)
                    job.delivered_to.add(number)
                elif not self._gsm.is_alive():
                    logger.error("SIM800C stopped responding while sending — will reconnect.")
                    self._ready = False
                    break
            if len(job.delivered_to) < len(self._phone_numbers):
                still_pending.append(job)
            if not self._ready:
                # Modem is gone — keep every job we haven't reached yet.
                still_pending.extend(self._pending[index + 1:])
                break
        self._pending = still_pending

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._pending.append(self._jobs.get(timeout=2.0))
                while True:   # drain anything else that arrived at the same time
                    self._pending.append(self._jobs.get_nowait())
            except queue.Empty:
                pass

            if not self._phone_numbers:
                self._pending.clear()
                continue

            if not self._ready:
                if self._pending or \
                        time.monotonic() - self._last_reconnect_attempt > GSM_HEALTH_CHECK_INTERVAL_SECONDS:
                    self._try_reconnect()
                continue

            if self._pending:
                self._deliver_pending()
            elif time.monotonic() - self._last_health_check > GSM_HEALTH_CHECK_INTERVAL_SECONDS:
                self._last_health_check = time.monotonic()
                if not self._gsm.is_alive():
                    logger.error("SIM800C failed its periodic health check — will reconnect.")
                    self._ready = False

        self._gsm.close()


# ---------------------------------------------------------------------------
# Gateway orchestration
# ---------------------------------------------------------------------------
class Gateway:
    def __init__(self, config: GatewayConfig):
        self._config = config
        self._packet_queue: "queue.Queue[tuple]" = queue.Queue()
        self._nodes: Dict[int, NodeState] = {}
        self._radio: Optional[Sx127xLoRa] = None
        self._alerts = AlertDispatcher(config.gsm_serial_port, config.alert_phone_numbers)
        self._stop_event = threading.Event()
        self._started_at = time.time()

    # -----------------------------------------------------------------
    def _on_lora_packet(self, payload: bytes, rssi_dbm: int, snr_db: float) -> None:
        # Runs on the gpiozero interrupt thread — hand off immediately.
        self._packet_queue.put((payload, rssi_dbm, snr_db, time.time()))

    def _node(self, node_id: int) -> NodeState:
        if node_id not in self._nodes:
            self._nodes[node_id] = NodeState(node_id=node_id, detector=NodeAnomalyDetector(node_id))
        return self._nodes[node_id]

    # -----------------------------------------------------------------
    def _restore_history(self) -> None:
        """Rebuild each vehicle's model history from the database, so a
        gateway restart doesn't drop every vehicle back into cold-start mode
        (where every motion event raises an alert)."""
        rows = telemetry_store.get_recent_telemetry_per_node(FEATURE_WINDOW_SIZE)
        for row in rows:
            t = Telemetry(
                node_id=row["node_id"], latitude=row["latitude"], longitude=row["longitude"],
                speed_kmph=row["speed_kmph"], heading_deg=row["heading_deg"],
                satellites=row["satellites"], motion_flag=bool(row["motion_flag"]),
                gps_valid=bool(row["gps_valid"]), battery_millivolts=row["battery_millivolts"],
                rssi_dbm=row["rssi_dbm"], snr_db=row["snr_db"],
                sequence=row["sequence_number"], packet_type=row["packet_type"],
                received_at=row["received_at"],
            )
            state = self._node(t.node_id)
            state.detector.learn(t, state.last_valid_fix)
            if t.gps_valid:
                state.last_valid_fix = t
            if t.sequence is not None:
                state.last_sequence = t.sequence
            state.last_packet = t
            state.last_seen = t.received_at

        for state in self._nodes.values():
            ctx_n = len(state.detector.context_model.history)
            mov_n = len(state.detector.movement_model.history)
            logger.info("Node %d: restored history — context model %d/%d, movement model %d/%d samples "
                        "(%s), last seen %s.",
                        state.node_id, ctx_n, CONTEXT_MIN_SAMPLES, mov_n, MOVEMENT_MIN_SAMPLES,
                        "learning period: every motion raises an alert" if ctx_n < CONTEXT_MIN_SAMPLES
                        else "trained", _fmt_time(state.last_seen))
            if self._started_at - state.last_seen > self._config.contact_timeout_seconds:
                # Already silent before the gateway started (e.g. an old test
                # node). Don't SMS about it on every restart — just note it.
                # Its next packet clears the flag like any other.
                state.contact_lost = True
                logger.warning("Node %d has not reported since %s — treating it as out of contact.",
                               state.node_id, _fmt_time(state.last_seen))
            else:
                # Recently active: give it a full timeout window from startup
                # before the loss-of-contact watchdog can fire for it.
                state.last_seen = max(state.last_seen, self._started_at)

    # -----------------------------------------------------------------
    def _check_sequence(self, state: NodeState, t: Telemetry) -> bool:
        """Returns False if the packet should be dropped (duplicate, or a
        suspected replay). Sequence numbers are NOT cryptographic — they stop
        accidental duplicates and naive replays, and they measure packet loss;
        real protection against forged packets needs payload authentication
        (AES/HMAC), which is listed as future work."""
        if t.sequence is None:
            return True
        last = state.last_sequence
        if last is None:
            state.last_sequence = t.sequence
            return True

        if t.sequence == last:
            logger.info("Node %d: duplicate packet seq=%d dropped.", t.node_id, t.sequence)
            return False

        if t.sequence < last:
            restarted = t.sequence <= SEQUENCE_RESET_THRESHOLD or (
                state.reset_candidate_sequence is not None and t.sequence > state.reset_candidate_sequence)
            if not restarted:
                logger.warning("Node %d: sequence went backwards (last %d, got %d) — possible replayed "
                               "packet, dropped. Will accept if the node keeps counting up from here.",
                               t.node_id, last, t.sequence)
                state.reset_candidate_sequence = t.sequence
                return False
            logger.info("Node %d: sequence restarted (%d -> %d) — node was power-cycled.",
                        t.node_id, last, t.sequence)
            state.reset_candidate_sequence = None
        elif t.sequence > last + 1:
            missed = t.sequence - last - 1
            state.packets_missed += missed
            logger.info("Node %d: %d packet(s) missed (seq %d -> %d).", t.node_id, missed, last, t.sequence)
            state.reset_candidate_sequence = None
        else:
            state.reset_candidate_sequence = None

        state.last_sequence = t.sequence
        return True

    # -----------------------------------------------------------------
    def _process_packet(self, payload: bytes, rssi_dbm: int, snr_db: float, received_at: float) -> None:
        t = parse_telemetry_packet(payload, rssi_dbm, snr_db)
        if t is None:
            return
        t.received_at = received_at

        if self._config.known_node_ids and t.node_id not in self._config.known_node_ids:
            logger.warning("Ignoring packet from unknown node %d (not in known_node_ids).", t.node_id)
            return

        state = self._node(t.node_id)
        if t.protocol_version == 1 and not state.warned_legacy_firmware:
            logger.warning("Node %d is running legacy v1 firmware — reflash it for sequence numbers "
                           "and immediate motion alerts.", t.node_id)
            state.warned_legacy_firmware = True

        if not self._check_sequence(state, t):
            return

        logger.info(
            "Node %d: %s seq=%s lat=%.6f lon=%.6f speed=%.1fkm/h motion=%d gpsValid=%d "
            "batt=%dmV rssi=%ddBm snr=%.1fdB",
            t.node_id, PACKET_TYPE_NAMES.get(t.packet_type, "v1"), t.sequence, t.latitude,
            t.longitude, t.speed_kmph, t.motion_flag, t.gps_valid, t.battery_millivolts,
            t.rssi_dbm, t.snr_db,
        )

        if state.contact_lost:
            logger.warning("Node %d: contact restored after %s of silence.",
                           t.node_id, _fmt_duration(t.received_at - state.last_seen))
            state.contact_lost = False
        state.last_seen = t.received_at
        state.last_packet = t

        previous_fix = state.last_valid_fix
        anomaly = state.detector.evaluate(t, previous_fix)
        if t.gps_valid:
            state.last_valid_fix = t

        telemetry_store.insert_telemetry({
            "node_id": t.node_id, "latitude": t.latitude, "longitude": t.longitude,
            "speed_kmph": t.speed_kmph, "heading_deg": t.heading_deg,
            "satellites": t.satellites, "motion_flag": t.motion_flag,
            "gps_valid": t.gps_valid, "battery_millivolts": t.battery_millivolts,
            "sequence_number": t.sequence, "packet_type": t.packet_type,
            "rssi_dbm": t.rssi_dbm, "snr_db": t.snr_db,
            "anomaly_score": anomaly["score"], "is_anomaly": anomaly["is_anomaly"],
            "received_at": t.received_at,
        })

        now = t.received_at
        if not anomaly["is_anomaly"]:
            # Say WHY no alert was raised, so a silent gateway is never a
            # mystery (e.g. the model has learned this pattern as normal).
            if anomaly["reason"] == "normal":
                logger.info("Node %d: judged normal by the Isolation Forest (score=%.3f) — no alert.",
                            t.node_id, anomaly["score"])
            else:
                ctx_n = len(state.detector.context_model.history)
                logger.info("Node %d: no motion, still learning (%d/%d samples) — no alert.",
                            t.node_id, ctx_n, CONTEXT_MIN_SAMPLES)
        if anomaly["is_anomaly"]:
            if state.cooldown_active(now):
                logger.info("Node %d: anomaly (%s) during alert cooldown — no new SMS.",
                            t.node_id, anomaly["reason"])
            else:
                logger.warning("Node %d: ANOMALY CONFIRMED (%s, score=%s).",
                               t.node_id, anomaly["reason"], anomaly["score"])
                fix = t if t.gps_valid else state.last_valid_fix
                self._alerts.submit(
                    node_id=t.node_id, reason=anomaly["reason"], score=anomaly["score"], fix=fix,
                    speed_kmph=t.speed_kmph,
                    message=format_anomaly_sms(t, anomaly["reason"], state.last_valid_fix),
                )
                state.last_alert_time = now
                # The alert only had a stale (or no) position — send one
                # follow-up as soon as a fresh fix arrives.
                if not t.gps_valid:
                    state.location_followup_until = now + LOCATION_FOLLOWUP_WINDOW_SECONDS
                return

        if t.gps_valid and state.location_followup_until > now:
            state.location_followup_until = 0.0
            self._alerts.submit(
                node_id=t.node_id, reason="location_update", score=None, fix=t,
                speed_kmph=t.speed_kmph, message=format_location_update_sms(t),
            )

    # -----------------------------------------------------------------
    def _check_contact(self) -> None:
        if not self._config.loss_of_contact_alerts:
            return
        now = time.time()
        for state in self._nodes.values():
            silent_for = now - state.last_seen
            if state.contact_lost or silent_for < self._config.contact_timeout_seconds:
                continue
            state.contact_lost = True
            logger.warning("Node %d: NO CONTACT for %s — raising loss-of-contact alert.",
                           state.node_id, _fmt_duration(silent_for))
            last = state.last_packet
            self._alerts.submit(
                node_id=state.node_id, reason="loss_of_contact", score=None,
                fix=state.last_valid_fix, speed_kmph=last.speed_kmph if last else 0.0,
                message=format_loss_of_contact_sms(state.node_id, silent_for / 60.0,
                                                   state.last_valid_fix),
            )

    # -----------------------------------------------------------------
    def _write_status(self) -> None:
        """Heartbeat for the dashboard's System page: proves the gateway
        process is alive and reports whether SMS alerts can be delivered."""
        telemetry_store.write_gateway_status({
            "started_at": self._started_at,
            "updated_at": time.time(),
            "gsm_ready": self._alerts.ready,
            "gsm_port": self._alerts.gsm_port,
            "alert_numbers": len(self._config.alert_phone_numbers),
            "pending_alerts": self._alerts.pending_count,
            "lora_packets": self._radio.packets_received if self._radio else 0,
            "lora_crc_errors": self._radio.crc_errors if self._radio else 0,
            "loss_of_contact_alerts": self._config.loss_of_contact_alerts,
        })

    # -----------------------------------------------------------------
    def run(self) -> None:
        logger.info("Starting Edge Gateway...")

        telemetry_store.init_db()
        logger.info("SQLite store ready at %s", telemetry_store.DEFAULT_DB_PATH)
        self._restore_history()

        self._radio = Sx127xLoRa(
            spi_bus=LORA_SPI_BUS, spi_device=LORA_SPI_DEVICE,
            reset_pin=LORA_RESET_PIN, dio0_pin=LORA_DIO0_PIN,
        )
        self._radio.open()
        self._radio.start_receiving(self._on_lora_packet)

        self._alerts.connect()
        self._alerts.start()
        self._write_status()

        logger.info("Gateway running — %d alert number(s), loss-of-contact alerts %s (%.0f min). "
                    "Listening for telemetry... (Ctrl+C to stop)",
                    len(self._config.alert_phone_numbers),
                    "ON" if self._config.loss_of_contact_alerts else "OFF",
                    self._config.contact_timeout_seconds / 60.0)

        last_watchdog = last_radio_check = time.monotonic()
        while not self._stop_event.is_set():
            try:
                payload, rssi_dbm, snr_db, received_at = self._packet_queue.get(timeout=1.0)
                self._process_packet(payload, rssi_dbm, snr_db, received_at)
            except queue.Empty:
                pass
            except Exception:
                # One bad packet (or a transient DB error) must not stop the
                # gateway from listening.
                logger.exception("Error while processing a packet — continuing.")

            now = time.monotonic()
            if now - last_watchdog >= WATCHDOG_INTERVAL_SECONDS:
                last_watchdog = now
                try:
                    self._check_contact()
                except Exception:
                    logger.exception("Loss-of-contact check failed — continuing.")
                try:
                    self._write_status()
                except Exception:
                    logger.exception("Could not write gateway status — continuing.")
            if now - last_radio_check >= RADIO_HEALTH_CHECK_INTERVAL_SECONDS:
                last_radio_check = now
                try:
                    self._radio.ensure_receiving()
                except Exception:
                    logger.exception("LoRa health check failed — continuing.")

    def request_stop(self) -> None:
        self._stop_event.set()

    def shutdown(self) -> None:
        logger.info("Stopping Edge Gateway...")
        self._alerts.stop()
        if self._alerts.is_alive():
            self._alerts.join(timeout=10.0)
        if self._radio is not None:
            try:
                self._radio.stop_receiving()
            finally:
                self._radio.close()
        logger.info("Gateway stopped cleanly.")


def main() -> int:
    gateway = Gateway(load_config())

    def _handle_signal(signum, frame):
        logger.info("Received signal %d — shutting down.", signum)
        gateway.request_stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    exit_code = 0
    try:
        gateway.run()
    except Exception:
        logger.exception("Fatal error in gateway — shutting down.")
        exit_code = 1
    finally:
        gateway.shutdown()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
