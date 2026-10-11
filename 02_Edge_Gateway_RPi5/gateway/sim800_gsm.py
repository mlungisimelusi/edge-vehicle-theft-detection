"""
============================================================================
 Edge-Based Vehicle Theft Detection System — LoRa-GSM Hybrid Network
 SIM800C GSM/GPRS AT-command driver — Raspberry Pi 5 / Ubuntu Linux
============================================================================
Talks to the SIM800C over the Pi's hardware UART (wired to GPIO14/15 — see
Phase1_Hardware_Wiring.md) using plain AT commands. Used ONLY as an
emergency failover channel: the Pi activates it to send an SMS alert once
the anomaly detector confirms a threat. It is not kept transmitting
continuously, matching the "GSM as failover, not primary link" design rule
for this project.

Reliability behaviour (checked, not assumed):
    - BOTH the serial device path AND the baud rate are auto-detected.
      Raspberry Pi OS ships a udev rule that symlinks /dev/serial0 to
      whichever UART is active; Ubuntu doesn't always include that rule,
      and the Pi 5's RP1 chip enumerates UARTs differently to older Pis
      besides — so the real device can end up as /dev/ttyAMA0,
      /dev/ttyAMA10, /dev/ttyS0, or a USB-serial adapter's /dev/ttyUSB0,
      depending on the board/image. All common candidates are tried.
    - Every AT command's response is read and checked for "OK" — a command
      that only appears to send (no exception) but never gets "OK" back is
      treated as a failure, not silently ignored.
    - Network registration and signal quality are checked before attempting
      to send, so a "no signal" condition is logged clearly instead of
      producing a confusing SMS-send failure with no context.
    - send_sms() retries on failure and reports which step failed.
============================================================================
"""

import glob
import logging
import time
from typing import List, Optional, Tuple

import serial

logger = logging.getLogger("sim800_gsm")

CANDIDATE_BAUD_RATES = [9600, 115200, 57600, 38400, 19200]
# Tried in this order. /dev/serial0 first since that's the "correct" alias
# when it exists; the rest cover Ubuntu-on-Pi5 and USB-adapter cases.
CANDIDATE_PORTS = [
    "/dev/serial0", "/dev/ttyAMA0", "/dev/ttyAMA10", "/dev/ttyS0",
    "/dev/ttyUSB0", "/dev/ttyUSB1",
]
CTRL_Z = "\x1a"
ESC = "\x1b"
# A single text-mode SMS holds 160 GSM 7-bit characters. The SIM800C does not
# split longer messages for you in text mode — AT+CMGS either errors or the
# network truncates — so callers must keep messages within this limit.
SMS_MAX_CHARS = 160
SMS_SEND_TIMEOUT_S = 60.0

# Unsolicited messages the SIM800C prints when it (re)starts or when its
# supply voltage misbehaves. Seeing one mid-command means the module reset.
REBOOT_MARKERS = ("Call Ready", "SMS Ready", "RDY", "NORMAL POWER DOWN",
                  "UNDER-VOLTAGE", "OVER-VOLTAGE")


def _module_rebooted(response: str) -> bool:
    return any(marker in response for marker in REBOOT_MARKERS)


class Sim800Gsm:
    def __init__(self, port: Optional[str] = None, timeout_s: float = 5.0):
        """port=None (default) auto-detects across CANDIDATE_PORTS. Pass an
        explicit path (e.g. "/dev/ttyAMA0") once you know which one your
        board actually uses, to skip the scan on every startup."""
        self._port_name = port
        self._timeout_s = timeout_s
        self._serial: Optional[serial.Serial] = None
        self._baud: Optional[int] = None

    # -----------------------------------------------------------------
    def open(self, port_candidates: List[str] = None, baud_candidates: List[int] = None) -> bool:
        """Open the serial port, auto-detecting the device path and/or baud
        rate by probing with plain "AT". Returns True once the module
        responds on some (port, baud) combination."""
        if self._port_name:
            port_candidates = [self._port_name]
        else:
            port_candidates = port_candidates or [p for p in CANDIDATE_PORTS if glob.glob(p)] or CANDIDATE_PORTS
        baud_candidates = baud_candidates or CANDIDATE_BAUD_RATES

        tried = []
        for port in port_candidates:
            for baud in baud_candidates:
                tried.append(f"{port}@{baud}")
                try:
                    self._serial = serial.Serial(port, baudrate=baud, timeout=self._timeout_s)
                except serial.SerialException as exc:
                    # Logged at WARNING, not DEBUG: a port that can't even be
                    # opened (permission denied, busy) is a different fault
                    # from a port that opens but gets no reply, and hiding it
                    # makes the two indistinguishable in the log.
                    hint = ""
                    if "Permission denied" in str(exc) or getattr(exc, "errno", None) == 13:
                        hint = (" — your user is not allowed to open this port. Fix with "
                                "`sudo usermod -aG dialout $USER`, then log out and back in.")
                    elif "busy" in str(exc).lower() or getattr(exc, "errno", None) == 16:
                        hint = " — another program (e.g. minicom) still has the port open."
                    logger.warning("Could not open %s at %d baud: %s%s", port, baud, exc, hint)
                    self._serial = None
                    # Every baud rate will fail the same way on this port.
                    break

                time.sleep(0.1)
                self._flush()
                ok, _ = self._send_raw("AT", expect="OK", timeout_s=1.0)
                if ok:
                    self._port_name = port
                    self._baud = baud
                    logger.info("SIM800C responding at %d baud on %s.", baud, port)
                    # Disable command echo so response parsing is clean.
                    self._send_raw("ATE0", expect="OK", timeout_s=1.0)
                    return True

                self._serial.close()
                self._serial = None

        logger.error(
            "SIM800C did not respond to AT on any of %s — check TXD/RXD wiring "
            "(SIM800C TXD -> Pi RXD, SIM800C RXD <- Pi TXD), that the module has "
            "its own adequate power supply (2A-capable, NOT the Pi's 5V/3.3V rail), "
            "that your user is in the 'dialout' group (see any 'Could not open' warnings "
            "above), and run `ls -l /dev/tty*` on the Pi to confirm the real UART device "
            "name if none of these matched.", tried,
        )
        return False

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    def reopen(self) -> bool:
        """Close and re-probe the module (same port/baud detection as open()).
        Used by the gateway to recover after the module was unpowered,
        reset, or not ready when the gateway started."""
        self.close()
        return self.open()

    @property
    def port_name(self) -> Optional[str]:
        return self._port_name

    def is_alive(self) -> bool:
        """Cheap health check: does the module still answer a plain AT?"""
        if self._serial is None:
            return False
        try:
            ok, _ = self._send_raw("AT", expect="OK", timeout_s=1.5)
            return ok
        except (serial.SerialException, OSError) as exc:
            logger.warning("SIM800C health check failed: %s", exc)
            return False

    def sim_ready(self) -> bool:
        """True if the SIM card is inserted and unlocked (AT+CPIN? -> READY).
        A missing or PIN-locked SIM otherwise shows up only as a vague
        'not registered on the network' failure."""
        ok, response = self._send_raw("AT+CPIN?", expect="+CPIN:", timeout_s=3.0)
        if ok and "READY" in response:
            return True
        logger.error("SIM800C SIM card not ready (AT+CPIN? -> %r). Check the SIM is inserted, "
                     "active, and has no PIN lock.", response.strip())
        return False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # -----------------------------------------------------------------
    def _flush(self) -> None:
        try:
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        except Exception:
            pass

    def _send_raw(self, command: str, expect: str = "OK", timeout_s: Optional[float] = None) -> Tuple[bool, str]:
        """Send a single AT command and read until `expect` appears, an
        "ERROR" is seen, or the timeout elapses. Returns (success, raw_response)."""
        if self._serial is None:
            return False, ""

        timeout_s = timeout_s if timeout_s is not None else self._timeout_s
        self._flush()
        self._serial.write((command + "\r\n").encode("ascii", errors="ignore"))

        deadline = time.monotonic() + timeout_s
        buffer = ""
        while time.monotonic() < deadline:
            chunk = self._serial.read(self._serial.in_waiting or 1)
            if chunk:
                buffer += chunk.decode("ascii", errors="ignore")
                if expect and expect in buffer:
                    return True, buffer
                if "ERROR" in buffer:
                    return False, buffer
            else:
                time.sleep(0.02)

        return False, buffer

    # -----------------------------------------------------------------
    def is_network_registered(self, timeout_s: float = 15.0) -> bool:
        """Polls AT+CREG? until the module reports home (,1) or roaming (,5)
        registration, or the timeout elapses."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            ok, response = self._send_raw("AT+CREG?", expect="+CREG:", timeout_s=2.0)
            if ok and ("+CREG: 0,1" in response or "+CREG: 0,5" in response
                       or "+CREG: 1,1" in response or "+CREG: 1,5" in response):
                return True
            time.sleep(1.0)

        logger.warning("SIM800C did not report network registration within %.0fs.", timeout_s)
        return False

    def signal_quality(self) -> Optional[int]:
        """Returns the RSSI in dBm (via AT+CSQ), or None if unreadable.
        CSQ value 0-31 maps to -113..-51 dBm; 99 means unknown/undetectable."""
        ok, response = self._send_raw("AT+CSQ", expect="+CSQ:", timeout_s=2.0)
        if not ok:
            return None
        try:
            after = response.split("+CSQ:")[1]
            csq_raw = int(after.strip().split(",")[0])
        except (IndexError, ValueError):
            return None
        if csq_raw == 99:
            return None
        return -113 + (csq_raw * 2)

    # -----------------------------------------------------------------
    def send_sms(self, phone_number: str, message: str, retries: int = 3, retry_delay_s: float = 3.0) -> bool:
        """Sends a text-mode SMS. Returns True only once the module confirms
        submission (+CMGS response), retrying the whole sequence on failure."""
        if self._serial is None:
            logger.error("send_sms() called before open() succeeded.")
            return False

        if len(message) > SMS_MAX_CHARS:
            logger.warning("SMS is %d chars (limit %d) — truncating so it can be sent as one SMS.",
                           len(message), SMS_MAX_CHARS)
            message = message[:SMS_MAX_CHARS]

        try:
            return self._send_sms_with_retries(phone_number, message, retries, retry_delay_s)
        except (serial.SerialException, OSError) as exc:
            # The port itself failed (module unpowered, cable pulled). Close it
            # so the gateway's recovery logic knows to reopen it.
            logger.error("Serial error while sending SMS to %s: %s", phone_number, exc)
            self.close()
            return False

    def _send_sms_with_retries(self, phone_number: str, message: str, retries: int,
                               retry_delay_s: float) -> bool:
        for attempt in range(1, retries + 1):
            ok, resp = self._send_raw("AT+CMGF=1", expect="OK", timeout_s=2.0)  # text mode
            if not ok:
                if _module_rebooted(resp) or "AT+CMGF" in resp:
                    # Command echo is back on (ATE0 is lost on restart) or boot
                    # messages are showing — the module restarted and must be
                    # re-initialised, not just retried.
                    logger.error("SMS attempt %d/%d: SIM800C has restarted (%r) — reconnecting.",
                                 attempt, retries, resp.strip())
                    self.close()
                    return False
                logger.error("SMS attempt %d/%d: AT+CMGF=1 (text mode) failed: %r", attempt, retries, resp)
                time.sleep(retry_delay_s)
                continue

            self._flush()
            self._serial.write(f'AT+CMGS="{phone_number}"\r\n'.encode("ascii", errors="ignore"))

            # Wait for the ">" prompt before writing the message body.
            prompt_deadline = time.monotonic() + 5.0
            got_prompt = False
            buffer = ""
            while time.monotonic() < prompt_deadline:
                chunk = self._serial.read(self._serial.in_waiting or 1)
                if chunk:
                    buffer += chunk.decode("ascii", errors="ignore")
                    if ">" in buffer:
                        got_prompt = True
                        break
                else:
                    time.sleep(0.02)

            if not got_prompt:
                logger.error("SMS attempt %d/%d: no '>' prompt from AT+CMGS (resp so far: %r)",
                             attempt, retries, buffer)
                self._serial.write(ESC.encode())  # abort
                time.sleep(retry_delay_s)
                continue

            self._serial.write(message.encode("ascii", errors="ignore") + CTRL_Z.encode())

            # SIMCom specifies up to 60 s for AT+CMGS to answer (network
            # dependent) — a shorter wait gives up on SMSes that would arrive.
            send_deadline = time.monotonic() + SMS_SEND_TIMEOUT_S
            send_buffer = ""
            while time.monotonic() < send_deadline:
                chunk = self._serial.read(self._serial.in_waiting or 1)
                if chunk:
                    send_buffer += chunk.decode("ascii", errors="ignore")
                    if "+CMGS:" in send_buffer and "OK" in send_buffer:
                        logger.info("SMS sent to %s (attempt %d/%d).", phone_number, attempt, retries)
                        return True
                    if "ERROR" in send_buffer or _module_rebooted(send_buffer):
                        break
                else:
                    time.sleep(0.05)

            if _module_rebooted(send_buffer):
                # The transmit burst (up to ~2 A) pulled the supply down and
                # the module restarted. Retrying immediately just repeats it;
                # close the port so the gateway reconnects cleanly once the
                # module has re-registered.
                logger.error(
                    "SMS attempt %d/%d: the SIM800C REBOOTED while transmitting (%r). This is a power "
                    "supply brown-out: the module needs a 3.7-4.2 V supply that can deliver 2 A peaks, "
                    "a >=1000 uF capacitor right at its VCC/GND pins, short thick power wires, and a "
                    "ground shared with the Pi. It cannot send SMS until this is fixed.",
                    attempt, retries, send_buffer.strip())
                self.close()
                return False

            logger.error("SMS attempt %d/%d failed. Module response: %r", attempt, retries, send_buffer)
            time.sleep(retry_delay_s)

        logger.error("SMS to %s FAILED after %d attempts — alert NOT delivered.", phone_number, retries)
        return False
