# Phase 1 — Hardware Wiring & Connections
## Edge-Based Vehicle Theft Detection System (LoRa-GSM Hybrid Network)

Two physical assemblies: the **Mobile Node** (in-vehicle, ESP32-based) and the **Edge Gateway** (base station, Raspberry Pi 5-based).

Radio module: **SX1278** (433 MHz variant, pin-compatible with SX1276 — same SPI register map, different RF front-end/antenna matching). GSM module: **SIM800C** (same AT-command UART interface as SIM800L; the wiring and power warnings below apply equally).

---

## 1. Mobile Node — ESP32 DevKit

### 1.1 Power rail
- MPU6050, SX1278, and the ESP32 logic are all **3.3V** — power them from the ESP32's 3.3V pin.
- The Neo-6M GPS module has an onboard LDO and accepts 3.3V–5V input; its TX/RX logic on most breakout boards is 3.3V-safe. Power it from **5V (VIN)** if your board has a marked 5V pin for stronger satellite acquisition, otherwise 3.3V works — just confirm your specific breakout's regulator rating before wiring.
- Add a **100–220 µF electrolytic capacitor** across the SX1278's VCC/GND pins, close to the module. LoRa TX current bursts (~120 mA) sag unregulated 3.3V rails on breadboards and cause silent transmit failures.
- Common GND across all four modules and the ESP32 is mandatory.

### 1.2 MPU6050 (Accelerometer, I2C address 0x68)

| MPU6050 Pin | ESP32 Pin | Notes |
|---|---|---|
| VCC | 3.3V | |
| GND | GND | |
| SDA | GPIO21 | I2C data |
| SCL | GPIO22 | I2C clock |
| INT | GPIO27 | Interrupt output → ESP32 deep-sleep wake source (RTC-capable GPIO) |
| AD0 | GND | Ties I2C address to 0x68 (tie to VCC instead for 0x69 — not used here) |

### 1.3 Neo-6M GPS (UART)

| Neo-6M Pin | ESP32 Pin | Notes |
|---|---|---|
| VCC | 5V (or 3.3V — see 1.1) | |
| GND | GND | |
| TX | GPIO16 (RX2) | GPS TX → ESP32 UART2 RX |
| RX | GPIO17 (TX2) | ESP32 UART2 TX → GPS RX |

Uses ESP32's **hardware UART2**, leaving UART0 (GPIO1/3) free for USB/serial-monitor debugging.

### 1.4 SX1278 LoRa Transceiver, 433 MHz (SPI)

| SX1278 Pin | ESP32 Pin | Notes |
|---|---|---|
| VCC | 3.3V | **Never 5V — will damage the module** |
| GND | GND | |
| SCK | GPIO18 | VSPI clock |
| MISO | GPIO19 | VSPI MISO |
| MOSI | GPIO23 | VSPI MOSI |
| NSS (CS) | GPIO5 | Chip select |
| RST | GPIO14 | Reset |
| DIO0 | GPIO26 | TxDone/RxDone IRQ |

Confirm the antenna is a 433 MHz-tuned whip/helical — an 868/915 MHz antenna on an SX1278 433 MHz module will transmit at a fraction of rated range even though the chip itself is software-tunable across bands.

### 1.5 ESP32 pin summary (no conflicts, no strapping pins used)

| Function | GPIO |
|---|---|
| I2C SDA / SCL (MPU6050) | 21 / 22 |
| MPU6050 INT (deep-sleep wake) | 27 |
| GPS UART2 RX / TX | 16 / 17 |
| LoRa SPI SCK/MISO/MOSI | 18 / 19 / 23 |
| LoRa NSS / RST / DIO0 | 5 / 14 / 26 |

GPIO0, 2, 12, 15 (boot-strapping pins) are deliberately left unused.

---

## 2. Edge Gateway — Raspberry Pi 5

### 2.1 SX1278 LoRa Transceiver, 433 MHz (SPI0)

| SX1278 Pin | Raspberry Pi 5 Pin (BCM / physical) | Notes |
|---|---|---|
| VCC | 3V3 — physical pin 1 | **3.3V only** |
| GND | GND — physical pin 6 | |
| SCK | GPIO11 (SCLK) — pin 23 | SPI0 |
| MISO | GPIO9 (MISO) — pin 21 | SPI0 |
| MOSI | GPIO10 (MOSI) — pin 19 | SPI0 |
| NSS (CS) | GPIO8 (CE0) — pin 24 | SPI0 chip-select 0 |
| RST | GPIO25 — pin 22 | General GPIO |
| DIO0 | GPIO24 — pin 18 | RX-done IRQ |

Enable SPI0 first: `sudo raspi-config` → Interface Options → SPI → Enable, then reboot.

### 2.2 SIM800C GSM/GPRS Module (UART)

| SIM800C Pin | Raspberry Pi 5 Pin (BCM / physical) | Notes |
|---|---|---|
| TXD | GPIO15 (RXD) — pin 10 | SIM800C TX → Pi RX |
| RXD | GPIO14 (TXD) — pin 8 | Pi TX → SIM800C RX |
| GND | GND — pin 14 (or any GND) | Common ground with Pi **and** the SIM800C's own power supply |
| VCC | **External 3.7–4.2V, 2A-capable supply** | See warning below |

**Critical power warning:** the SIM800C draws current spikes up to ~2A during GSM transmit bursts. It must **not** be powered from the Pi's 5V or 3.3V rail — this will brown-out the Pi. Use a dedicated LiPo battery (3.7V) or a buck converter rated ≥2A, with a large (≥1000 µF) capacitor across VCC/GND at the module.

Free the Pi's UART for the SIM800C:
```bash
sudo raspi-config
# Interface Options → Serial Port
#   "Login shell over serial?"  -> No
#   "Serial port hardware enabled?" -> Yes
sudo reboot
```
This exposes `/dev/serial0` (GPIO14/15) for exclusive use by the SIM800C, un-occupied by the Linux console.

### 2.3 Raspberry Pi 5 pin summary

| Function | BCM GPIO | Physical Pin |
|---|---|---|
| LoRa SPI0 SCK/MISO/MOSI | 11 / 9 / 10 | 23 / 21 / 19 |
| LoRa NSS (CE0) / RST / DIO0 | 8 / 25 / 24 | 24 / 22 / 18 |
| SIM800C TXD→RXD / RXD←TXD | 14 / 15 | 8 / 10 |

---

## 3. Frequency & regulatory note

This build uses **433 MHz** (the SX1278 variant), part of South Africa's license-exempt SRD allocations under ICASA (the 433.05–434.79 MHz band is a commonly-used unlicensed ISM band, same one used by LPD433/PMR-adjacent short-range devices). Note this differs from an earlier 868 MHz / SX1276 draft of this project — if any modules were already purchased for 868 MHz, they are **not** interchangeable with 433 MHz hardware (different antenna matching network on the PCB), even though both chips share the same SPI command set. Verify current ICASA type-approval and gazette listings before deployment, since exemption schedules are periodically updated.

LoRa radio parameters (spreading factor, bandwidth, coding rate, TX power) are configured identically to the 868 MHz version in firmware — only the centre frequency constant changes.
