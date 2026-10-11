"""
============================================================================
 Edge-Based Vehicle Theft Detection System — LoRa-GSM Hybrid Network
 SX1278 (433 MHz) LoRa receive-only driver — Raspberry Pi 5 / Ubuntu Linux
============================================================================
This is a from-scratch, register-level SX127x driver (no third-party LoRa
library) written to be a byte-for-byte protocol match for the ESP32 mobile
node's firmware, which uses the "LoRa" library by Sandeep Mistry
(https://github.com/sandeepmistry/arduino-LoRa) with these settings:

    frequency        = 433 MHz
    spreading factor = 9
    bandwidth        = 125 kHz
    coding rate      = 4/5
    header mode      = explicit (default — never switched to implicit)
    CRC              = enabled (LoRa.enableCrc() is called)
    sync word        = 0x12 (the chip's power-on-reset default — the ESP32
                        firmware never calls setSyncWord(), so it is never
                        changed from the SX127x's factory default)
    preamble length  = 8 symbols (also the power-on-reset default)

Every constant and register value below was cross-checked directly against
the arduino-LoRa source (LoRa.cpp/LoRa.h) rather than assumed, specifically
to avoid the kind of silent protocol mismatch that would make the Pi never
receive a single packet despite both radios being individually healthy.

Hardware interface notes (see Phase1_Hardware_Wiring.md):
    - SPI0 (hardware chip-select CE0 on GPIO8/pin24) is used for NSS, so
      spidev toggles chip-select automatically around every transfer — no
      manual GPIO bit-banging for NSS is needed or done here.
    - RST (GPIO25) and DIO0 (GPIO24) are handled via gpiozero, which uses
      the "lgpio" pin factory on Raspberry Pi 5 (the older RPi.GPIO library
      does not support the Pi 5's RP1 I/O controller).
============================================================================
"""

import logging
import threading
import time
from typing import Callable, Optional

import spidev
from gpiozero import DigitalOutputDevice, DigitalInputDevice

logger = logging.getLogger("sx127x_lora")

# ---------------------------------------------------------------------------
# SX127x registers (Semtech SX1276/77/78/79 datasheet register map)
# ---------------------------------------------------------------------------
REG_FIFO = 0x00
REG_OP_MODE = 0x01
REG_FRF_MSB = 0x06
REG_FRF_MID = 0x07
REG_FRF_LSB = 0x08
REG_PA_CONFIG = 0x09
REG_LNA = 0x0C
REG_FIFO_ADDR_PTR = 0x0D
REG_FIFO_TX_BASE_ADDR = 0x0E
REG_FIFO_RX_BASE_ADDR = 0x0F
REG_FIFO_RX_CURRENT_ADDR = 0x10
REG_IRQ_FLAGS = 0x12
REG_RX_NB_BYTES = 0x13
REG_PKT_SNR_VALUE = 0x19
REG_PKT_RSSI_VALUE = 0x1A
REG_RSSI_VALUE = 0x1B
REG_MODEM_CONFIG_1 = 0x1D
REG_MODEM_CONFIG_2 = 0x1E
REG_PREAMBLE_MSB = 0x20
REG_PREAMBLE_LSB = 0x21
REG_PAYLOAD_LENGTH = 0x22
REG_MODEM_CONFIG_3 = 0x26
REG_DETECTION_OPTIMIZE = 0x31
REG_DETECTION_THRESHOLD = 0x37
REG_SYNC_WORD = 0x39
REG_DIO_MAPPING_1 = 0x40
REG_VERSION = 0x42

# Operating modes (written to REG_OP_MODE, always OR'd with LONG_RANGE_MODE)
MODE_LONG_RANGE_MODE = 0x80
MODE_SLEEP = 0x00
MODE_STDBY = 0x01
MODE_RX_CONTINUOUS = 0x05

# IRQ flag bits (REG_IRQ_FLAGS)
IRQ_RX_DONE_MASK = 0x40
IRQ_PAYLOAD_CRC_ERROR_MASK = 0x20

RF_MID_BAND_THRESHOLD_HZ = 525_000_000
RSSI_OFFSET_HF_PORT = 157
RSSI_OFFSET_LF_PORT = 164

EXPECTED_CHIP_VERSION = 0x12  # REG_VERSION readback for genuine SX127x silicon

# ---------------------------------------------------------------------------
# Radio parameters — MUST match the ESP32 firmware's LORA_* #defines exactly
# ---------------------------------------------------------------------------
DEFAULT_FREQUENCY_HZ = 433_000_000
DEFAULT_SPREADING_FACTOR = 9
DEFAULT_BANDWIDTH_HZ = 125_000
DEFAULT_CODING_RATE_DENOM = 5      # 4/5
DEFAULT_SYNC_WORD = 0x12           # SX127x power-on-reset default (unchanged by ESP32 firmware)
DEFAULT_PREAMBLE_LENGTH = 8        # SX127x power-on-reset default


class Sx127xLoRa:
    """Receive-only SX1278 (433 MHz) driver for continuous LoRa listening."""

    def __init__(
        self,
        spi_bus: int = 0,
        spi_device: int = 0,
        spi_speed_hz: int = 8_000_000,
        nss_is_hardware_cs: bool = True,
        reset_pin: int = 25,
        dio0_pin: int = 24,
        frequency_hz: int = DEFAULT_FREQUENCY_HZ,
        spreading_factor: int = DEFAULT_SPREADING_FACTOR,
        bandwidth_hz: int = DEFAULT_BANDWIDTH_HZ,
        coding_rate_denom: int = DEFAULT_CODING_RATE_DENOM,
        sync_word: int = DEFAULT_SYNC_WORD,
        preamble_length: int = DEFAULT_PREAMBLE_LENGTH,
    ):
        self._spi_bus = spi_bus
        self._spi_device = spi_device
        self._spi_speed_hz = spi_speed_hz
        self._frequency_hz = frequency_hz
        self._spreading_factor = spreading_factor
        self._bandwidth_hz = bandwidth_hz
        self._coding_rate_denom = coding_rate_denom
        self._sync_word = sync_word
        self._preamble_length = preamble_length

        self._spi: Optional[spidev.SpiDev] = None
        self._reset_gpio = DigitalOutputDevice(reset_pin, initial_value=True)
        self._dio0_gpio = DigitalInputDevice(dio0_pin, pull_up=False)
        self._on_receive: Optional[Callable[[bytes, int, float], None]] = None
        # The DIO0 interrupt handler runs on gpiozero's thread while the
        # gateway's main thread calls ensure_receiving() — this lock stops
        # the two from interleaving SPI register sequences.
        self._lock = threading.RLock()
        self._packets_received = 0
        self._crc_errors = 0

    @property
    def packets_received(self) -> int:
        return self._packets_received

    @property
    def crc_errors(self) -> int:
        return self._crc_errors

    # -----------------------------------------------------------------
    # Low-level register access. NSS/CS is toggled automatically by the
    # kernel SPI driver around each xfer2() call (hardware CE0), matching
    # the ESP32 side's per-byte-pair SPI transaction semantics.
    # -----------------------------------------------------------------
    def _read_register(self, address: int) -> int:
        with self._lock:
            result = self._spi.xfer2([address & 0x7F, 0x00])
            return result[1]

    def _write_register(self, address: int, value: int) -> None:
        with self._lock:
            self._spi.xfer2([address | 0x80, value & 0xFF])

    # -----------------------------------------------------------------
    def open(self) -> None:
        """Reset the module, verify it's a genuine SX127x, and configure it
        to match the ESP32 mobile node's LoRa parameters exactly."""
        self._spi = spidev.SpiDev()
        self._spi.open(self._spi_bus, self._spi_device)
        self._spi.max_speed_hz = self._spi_speed_hz
        self._spi.mode = 0b00  # SPI_MODE0, matching Sandeep Mistry's library

        self._hardware_reset()

        version = self._read_register(REG_VERSION)
        if version != EXPECTED_CHIP_VERSION:
            self._spi.close()
            self._spi = None
            raise RuntimeError(
                f"SX127x not detected (REG_VERSION=0x{version:02X}, expected "
                f"0x{EXPECTED_CHIP_VERSION:02X}) — check SPI wiring "
                f"(SCK=GPIO11/pin23, MISO=GPIO9/pin21, MOSI=GPIO10/pin19, "
                f"NSS=GPIO8/CE0/pin24) and that SPI0 is enabled."
            )
        logger.info("SX127x detected, REG_VERSION=0x%02X", version)

        self._sleep()
        self._set_frequency(self._frequency_hz)

        self._write_register(REG_FIFO_TX_BASE_ADDR, 0x00)
        self._write_register(REG_FIFO_RX_BASE_ADDR, 0x00)

        # LNA boost
        self._write_register(REG_LNA, self._read_register(REG_LNA) | 0x03)

        # Auto AGC on, LDO optimize off (matches arduino-LoRa's begin() +
        # setLdoFlag() outcome at SF9/BW125kHz — symbol duration ~4ms, well
        # under the 16ms LDO threshold)
        self._write_register(REG_MODEM_CONFIG_3, 0x04)

        self._set_spreading_factor(self._spreading_factor)
        self._set_signal_bandwidth(self._bandwidth_hz)
        self._set_coding_rate(self._coding_rate_denom)
        self._set_preamble_length(self._preamble_length)
        self._set_sync_word(self._sync_word)
        self._enable_crc()
        self._set_explicit_header_mode()

        self._idle()
        logger.info(
            "SX127x configured: freq=%.3fMHz SF=%d BW=%dHz CR=4/%d sync=0x%02X preamble=%d",
            self._frequency_hz / 1e6, self._spreading_factor, self._bandwidth_hz,
            self._coding_rate_denom, self._sync_word, self._preamble_length,
        )

    def close(self) -> None:
        try:
            if self._spi is not None:
                self._sleep()
        except Exception:
            pass
        if self._spi is not None:
            self._spi.close()
        self._dio0_gpio.close()
        self._reset_gpio.close()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # -----------------------------------------------------------------
    def _hardware_reset(self) -> None:
        self._reset_gpio.off()
        time.sleep(0.01)
        self._reset_gpio.on()
        time.sleep(0.01)

    def _sleep(self) -> None:
        self._write_register(REG_OP_MODE, MODE_LONG_RANGE_MODE | MODE_SLEEP)

    def _idle(self) -> None:
        self._write_register(REG_OP_MODE, MODE_LONG_RANGE_MODE | MODE_STDBY)

    def _set_frequency(self, frequency_hz: int) -> None:
        frf = (frequency_hz << 19) // 32_000_000
        self._write_register(REG_FRF_MSB, (frf >> 16) & 0xFF)
        self._write_register(REG_FRF_MID, (frf >> 8) & 0xFF)
        self._write_register(REG_FRF_LSB, frf & 0xFF)

    def _set_spreading_factor(self, sf: int) -> None:
        sf = max(6, min(12, sf))
        if sf == 6:
            self._write_register(REG_DETECTION_OPTIMIZE, 0xC5)
            self._write_register(REG_DETECTION_THRESHOLD, 0x0C)
        else:
            self._write_register(REG_DETECTION_OPTIMIZE, 0xC3)
            self._write_register(REG_DETECTION_THRESHOLD, 0x0A)

        config2 = self._read_register(REG_MODEM_CONFIG_2)
        self._write_register(REG_MODEM_CONFIG_2, (config2 & 0x0F) | ((sf << 4) & 0xF0))

    def _bandwidth_to_code(self, bandwidth_hz: float) -> int:
        table = [7.8E3, 10.4E3, 15.6E3, 20.8E3, 31.25E3, 41.7E3, 62.5E3, 125E3, 250E3]
        for code, upper_bound in enumerate(table):
            if bandwidth_hz <= upper_bound:
                return code
        return 9  # 500 kHz

    def _set_signal_bandwidth(self, bandwidth_hz: float) -> None:
        code = self._bandwidth_to_code(bandwidth_hz)
        config1 = self._read_register(REG_MODEM_CONFIG_1)
        self._write_register(REG_MODEM_CONFIG_1, (config1 & 0x0F) | (code << 4))

    def _set_coding_rate(self, denominator: int) -> None:
        denominator = max(5, min(8, denominator))
        cr = denominator - 4
        config1 = self._read_register(REG_MODEM_CONFIG_1)
        self._write_register(REG_MODEM_CONFIG_1, (config1 & 0xF1) | (cr << 1))

    def _set_preamble_length(self, length: int) -> None:
        self._write_register(REG_PREAMBLE_MSB, (length >> 8) & 0xFF)
        self._write_register(REG_PREAMBLE_LSB, length & 0xFF)

    def _set_sync_word(self, sync_word: int) -> None:
        self._write_register(REG_SYNC_WORD, sync_word & 0xFF)

    def _enable_crc(self) -> None:
        config2 = self._read_register(REG_MODEM_CONFIG_2)
        self._write_register(REG_MODEM_CONFIG_2, config2 | 0x04)

    def _set_explicit_header_mode(self) -> None:
        config1 = self._read_register(REG_MODEM_CONFIG_1)
        self._write_register(REG_MODEM_CONFIG_1, config1 & 0xFE)

    # -----------------------------------------------------------------
    def _packet_rssi(self) -> int:
        raw = self._read_register(REG_PKT_RSSI_VALUE)
        offset = RSSI_OFFSET_LF_PORT if self._frequency_hz < RF_MID_BAND_THRESHOLD_HZ else RSSI_OFFSET_HF_PORT
        return raw - offset

    def _packet_snr(self) -> float:
        raw = self._read_register(REG_PKT_SNR_VALUE)
        # signed 8-bit, 0.25 dB per LSB (matches arduino-LoRa's packetSnr())
        signed = raw - 256 if raw > 127 else raw
        return signed * 0.25

    def _read_fifo_packet(self, length: int) -> bytes:
        data = bytearray(length)
        for i in range(length):
            data[i] = self._read_register(REG_FIFO)
        return bytes(data)

    def _handle_dio0_rise(self) -> None:
        with self._lock:
            irq_flags = self._read_register(REG_IRQ_FLAGS)
            self._write_register(REG_IRQ_FLAGS, irq_flags)  # clear (write-1-to-clear)

            if irq_flags & IRQ_PAYLOAD_CRC_ERROR_MASK:
                self._crc_errors += 1
                logger.warning("LoRa packet dropped: CRC error (RSSI=%d dBm)", self._packet_rssi())
                return

            if not (irq_flags & IRQ_RX_DONE_MASK):
                return

            packet_length = self._read_register(REG_RX_NB_BYTES)
            current_addr = self._read_register(REG_FIFO_RX_CURRENT_ADDR)
            self._write_register(REG_FIFO_ADDR_PTR, current_addr)

            payload = self._read_fifo_packet(packet_length)
            rssi = self._packet_rssi()
            snr = self._packet_snr()
            self._packets_received += 1

        # Callback runs OUTSIDE the lock — it only queues the packet, but
        # there's no reason to hold the SPI lock while it does.
        if self._on_receive:
            try:
                self._on_receive(payload, rssi, snr)
            except Exception:
                logger.exception("Unhandled exception in LoRa receive callback")

    # -----------------------------------------------------------------
    def start_receiving(self, callback: Callable[[bytes, int, float], None]) -> None:
        """Enter continuous-RX mode and invoke callback(payload_bytes, rssi_dbm,
        snr_db) from a GPIO-interrupt context every time a valid (CRC-OK)
        packet arrives on DIO0."""
        self._on_receive = callback

        self._write_register(REG_DIO_MAPPING_1, 0x00)  # DIO0 -> RxDone
        self._set_explicit_header_mode()
        self._write_register(REG_FIFO_ADDR_PTR, 0x00)
        self._write_register(REG_OP_MODE, MODE_LONG_RANGE_MODE | MODE_RX_CONTINUOUS)

        self._dio0_gpio.when_activated = self._handle_dio0_rise
        logger.info("LoRa receiver armed — listening continuously on DIO0 (GPIO24) interrupt.")

    def ensure_receiving(self) -> bool:
        """Health check, called periodically from the gateway's main loop.
        Verifies the chip still answers on SPI and is still in continuous-RX
        mode; if it has dropped out (brown-out reset, SPI glitch, ESD), it is
        fully reconfigured and re-armed. Returns True if the radio was
        healthy, False if it had to be recovered."""
        if self._spi is None or self._on_receive is None:
            return False
        with self._lock:
            version = self._read_register(REG_VERSION)
            op_mode = self._read_register(REG_OP_MODE)
            if version == EXPECTED_CHIP_VERSION and op_mode == (MODE_LONG_RANGE_MODE | MODE_RX_CONTINUOUS):
                return True

            logger.warning(
                "LoRa radio left continuous-RX mode (REG_VERSION=0x%02X, REG_OP_MODE=0x%02X) — "
                "reinitialising and re-arming the receiver.", version, op_mode)
            callback = self._on_receive
            self._dio0_gpio.when_activated = None
            self._spi.close()
            self._spi = None
        try:
            self.open()
            self.start_receiving(callback)
            logger.info("LoRa receiver recovered.")
        except Exception:
            logger.exception("LoRa receiver recovery failed — will retry on the next health check.")
            self._on_receive = callback
        return False

    def stop_receiving(self) -> None:
        self._dio0_gpio.when_activated = None
        if self._spi is not None:
            self._idle()
