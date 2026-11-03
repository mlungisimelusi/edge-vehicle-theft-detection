"""
============================================================================
 Edge-Based Vehicle Theft Detection System — LoRa packet encryption (v3)
============================================================================
Every telemetry packet from the ESP32 is encrypted AND authenticated with
AES-128-GCM, using a 128-bit key shared by the node and the gateway.

    Confidentiality  position, speed and motion data can't be read by anyone
                     listening on 433 MHz.
    Authenticity     the 16-byte GCM tag proves the packet was made with the
                     key; a forged or modified packet fails verification and
                     is dropped.
    Freshness        the packet's (boot epoch, sequence) pair must always go
                     up. Because it is authenticated, an attacker can't change
                     it, so a recorded-and-replayed packet is rejected with
                     certainty (unlike the v2 heuristic).

Packet v3 layout (51 bytes, little-endian, packed):
    header (14 bytes, sent in clear, authenticated as GCM "additional data")
        uint8  protocolVersion = 3
        uint8  packetType
        uint32 nodeId
        uint32 bootEpoch   increments on every power-on (stored in ESP32 flash)
        uint32 sequence    increments on every packet, restarts each boot
    ciphertext (21 bytes) — the encrypted body:
        float lat, float lon, float speedKmph, float headingDeg,
        uint8 satellites, uint8 motionFlag, uint8 gpsValid, uint16 batteryMv
    tag (16 bytes) — GCM authentication tag

Nonce (12 bytes) = nodeId || bootEpoch || sequence. GCM must never reuse a
nonce with the same key: the sequence never repeats within a boot, and the
boot epoch (kept in flash, not RAM) never repeats across power cycles, so
every packet's nonce is unique.

The key lives in gateway/secrets.json (created by generate_secrets.py, which
also writes the matching lora_key.h for the ESP32). Neither file is in Git.
============================================================================
"""

import json
import os
import struct
from typing import Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PROTOCOL_VERSION_ENCRYPTED = 3
HEADER_FORMAT = "<BBIII"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)          # 14
BODY_FORMAT = "<ffffBBBH"
BODY_SIZE = struct.calcsize(BODY_FORMAT)              # 21
TAG_SIZE = 16
PACKET_SIZE = HEADER_SIZE + BODY_SIZE + TAG_SIZE      # 51

SECRETS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets.json")


class AuthenticationError(Exception):
    """The packet failed GCM verification: forged, altered, corrupted, or
    made with a different key."""


def nonce_for(node_id: int, boot_epoch: int, sequence: int) -> bytes:
    return struct.pack("<III", node_id, boot_epoch, sequence)


def encrypt_packet(key: bytes, packet_type: int, node_id: int, boot_epoch: int, sequence: int,
                   lat: float, lon: float, speed: float, heading: float, satellites: int,
                   motion: bool, gps_valid: bool, battery_mv: int) -> bytes:
    """Build a v3 packet exactly as the ESP32 firmware does (used by the
    simulation and the tests)."""
    header = struct.pack(HEADER_FORMAT, PROTOCOL_VERSION_ENCRYPTED, packet_type, node_id,
                         boot_epoch, sequence)
    body = struct.pack(BODY_FORMAT, lat, lon, speed, heading, satellites,
                       1 if motion else 0, 1 if gps_valid else 0, battery_mv)
    return header + AESGCM(key).encrypt(nonce_for(node_id, boot_epoch, sequence), body, header)


def decrypt_packet(key: bytes, payload: bytes) -> Tuple[tuple, tuple]:
    """Returns (header_fields, body_fields). Raises AuthenticationError if
    the tag doesn't verify, ValueError if the packet is malformed."""
    if len(payload) != PACKET_SIZE:
        raise ValueError(f"v3 packet must be {PACKET_SIZE} bytes, got {len(payload)}")
    header = payload[:HEADER_SIZE]
    version, packet_type, node_id, boot_epoch, sequence = struct.unpack(HEADER_FORMAT, header)
    if version != PROTOCOL_VERSION_ENCRYPTED:
        raise ValueError(f"not a v3 packet (version {version})")
    try:
        body = AESGCM(key).decrypt(nonce_for(node_id, boot_epoch, sequence), payload[HEADER_SIZE:], header)
    except InvalidTag as exc:
        raise AuthenticationError(f"authentication failed for packet claiming node {node_id}") from exc
    return (version, packet_type, node_id, boot_epoch, sequence), struct.unpack(BODY_FORMAT, body)


def parse_key(hex_text: str) -> bytes:
    key = bytes.fromhex(hex_text.strip())
    if len(key) != 16:
        raise ValueError("the LoRa key must be 16 bytes (32 hex characters)")
    return key


def load_secrets(path: str = SECRETS_PATH) -> dict:
    """Reads gateway/secrets.json. The key can also come from the
    VTD_LORA_KEY environment variable. Returns {} if neither exists."""
    secrets = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            secrets = json.load(fh)
    if os.environ.get("VTD_LORA_KEY"):
        secrets["lora_aes_key_hex"] = os.environ["VTD_LORA_KEY"]
    return secrets


def load_key(path: str = SECRETS_PATH) -> Optional[bytes]:
    hex_key = load_secrets(path).get("lora_aes_key_hex")
    return parse_key(hex_key) if hex_key else None
