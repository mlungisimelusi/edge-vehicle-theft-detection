/*
 * ============================================================================
 *  Edge-Based Vehicle Theft Detection System — LoRa-GSM Hybrid Network
 *  MOBILE NODE FIRMWARE (ESP32)
 * ============================================================================
 *  Wake sources : MPU6050 motion interrupt (GPIO27) OR periodic RTC timer.
 *  Sensors      : MPU6050 (I2C 0x68), Neo-6M GPS (UART2)
 *  Radio        : SX1278 LoRa, 433 MHz (VSPI) — sends telemetry to the RPi5 gateway.
 *
 *  Pin map (see Phase1_Hardware_Wiring.md for full wiring — UNCHANGED):
 *    MPU6050  SDA=21  SCL=22  INT=27
 *    GPS      RX2=16  TX2=17  (9600 baud NMEA)
 *    SX1278   SCK=18  MISO=19  MOSI=23  NSS=5  RST=14  DIO0=26
 *    Battery  ADC=34
 *
 *  MPU6050 driver: NO external MPU6050 library is used. The sensor is driven
 *  entirely with direct I2C register reads/writes over Wire.h — this mirrors
 *  the exact bring-up sequence that was bench-verified against the physical
 *  module (I2C ACK detection + PWR_MGMT_1 wake), so there is no library
 *  version/dependency surface left to fail. Register map is the public
 *  InvenSense MPU-6000/6050 register map (RM-MPU-6000A-00).
 *
 *  DUAL-SILICON SUPPORT: many "MPU6050 GY-521" breakout boards on the market
 *  actually ship an MPU6500 die (same footprint, same base I2C address 0x68,
 *  same ACCEL/GYRO output registers) instead of a genuine MPU6050. The two
 *  chips are IDENTICAL for reading acceleration/gyro data, but their MOTION
 *  INTERRUPT hardware is completely different and NOT register-compatible:
 *    - True MPU6050: legacy HPF-based comparator (MOT_THR/MOT_DUR/
 *      MOT_DETECT_CTRL registers), WHO_AM_I reads 0x68.
 *    - MPU6500 (and MPU9250): "Wake-on-Motion" comparator (WOM_THR/
 *      LP_ACCEL_ODR/ACCEL_INTEL_CTRL registers, which happen to sit at some
 *      of the SAME register addresses but mean something different),
 *      WHO_AM_I reads 0x70.
 *  This firmware reads WHO_AM_I at boot and configures whichever motion
 *  interrupt scheme matches the silicon actually detected, so it works on
 *  either chip without the user needing to know which one they were shipped.
 *
 *  Required libraries (Arduino Library Manager):
 *    - "TinyGPSPlus" by Mikal Hart
 *    - "LoRa" by Sandeep Mistry
 *    - Wire, SPI, HardwareSerial — bundled with the ESP32 Arduino core.
 *
 *  Board: ESP32 Dev Module (Arduino-ESP32 core)
 *
 *  Wake flow (v2):
 *    MOTION wake : init MPU + LoRa -> send an IMMEDIATE "motion" packet with
 *                  the last cached position (so the gateway can alert within
 *                  ~1 s instead of waiting up to 45 s for GPS) -> acquire GPS
 *                  -> send a "motion follow-up" packet with the fresh fix.
 *    TIMER wake  : acquire GPS -> send one "heartbeat" packet.
 *    Both        : put the SX1278 into LoRa sleep before ESP32 deep sleep (in
 *                  standby it would keep drawing ~1.6 mA the whole time).
 *
 *  Every packet carries a protocol version, a packet type and a sequence
 *  number kept in RTC memory. The gateway uses the sequence number to drop
 *  duplicates, flag sequence resets/replays, and compute Packet Delivery
 *  Ratio (PDR) from the gaps.
 *
 *  Reliability notes:
 *    - MPU6050 bring-up is retried (MPU_INIT_RETRIES attempts). If the sensor
 *      never ACKs on the bus, the node does NOT halt — it disarms the motion
 *      wake source for this boot and falls back to timer-only heartbeat
 *      reporting, so a single bad joint/connector never bricks tracking.
 *    - LoRa bring-up is retried (LORA_INIT_RETRIES attempts). If the radio
 *      never responds, telemetry for this cycle is skipped (logged), and the
 *      device still sleeps and retries fresh on the next wake.
 * ============================================================================
 */

#include <Wire.h>
#include <SPI.h>
#include <LoRa.h>
#include <TinyGPSPlus.h>
#include <HardwareSerial.h>
#include "esp_sleep.h"

// This firmware targets the classic ESP32 (ESP32-WROOM / DevKit): the pin
// map (GPIO16/17/21/22/23/26/27/34) and the EXT0 motion wake-up only exist
// on that chip. Fail with a clear message instead of an obscure one if a
// different board (ESP32-C3/C6/H2/S2...) is selected in the Arduino IDE.
#if !defined(CONFIG_IDF_TARGET_ESP32)
#error "Wrong board selected: choose Tools > Board > esp32 > 'ESP32 Dev Module'. This firmware's pin map and EXT0 motion wake-up only exist on the classic ESP32."
#endif

// ---------------------------------------------------------------------------
// Pin definitions — DO NOT CHANGE (fixed by existing hardware wiring)
// ---------------------------------------------------------------------------
#define I2C_SDA_PIN         21
#define I2C_SCL_PIN         22
#define MPU_INT_PIN         27      // RTC-capable GPIO, used for ext0 wake

#define GPS_RX_PIN          16      // ESP32 RX2 <- GPS TX
#define GPS_TX_PIN          17      // ESP32 TX2 -> GPS RX
#define GPS_BAUD            9600

#define LORA_SCK_PIN        18
#define LORA_MISO_PIN       19
#define LORA_MOSI_PIN       23
#define LORA_NSS_PIN        5
#define LORA_RST_PIN        14
#define LORA_DIO0_PIN       26

// ---------------------------------------------------------------------------
// MPU6050 register map (InvenSense RM-MPU-6000A-00) — used for direct I2C access
// ---------------------------------------------------------------------------
#define MPU6050_I2C_ADDR       0x68   // AD0 tied to GND

#define MPU_REG_ACCEL_CONFIG   0x1C
#define MPU_REG_MOT_THR        0x1F   // == WOM_THR on MPU6500
#define MPU_REG_LP_ACCEL_ODR   0x1E   // MPU6500/9250 only
#define MPU_REG_MOT_DUR        0x20   // true MPU6050 only (reserved/unused on MPU6500)
#define MPU_REG_INT_PIN_CFG    0x37
#define MPU_REG_INT_ENABLE     0x38
#define MPU_REG_INT_STATUS     0x3A
#define MPU_REG_ACCEL_XOUT_H   0x3B
#define MPU_REG_GYRO_XOUT_H    0x43
#define MPU_REG_MOT_DETECT_STATUS 0x61   // true MPU6050 only
#define MPU_REG_SIGNAL_PATH_RESET 0x68
#define MPU_REG_MOT_DETECT_CTRL   0x69   // == ACCEL_INTEL_CTRL on MPU6500
#define MPU_REG_PWR_MGMT_1     0x6B
#define MPU_REG_PWR_MGMT_2     0x6C
#define MPU_REG_WHO_AM_I       0x75

#define MPU_WHOAMI_MPU6050     0x68
#define MPU_WHOAMI_MPU6500     0x70

// INT_PIN_CFG (0x37): LATCH_INT_EN=1 (bit5), INT_RD_CLEAR=0 (bit4 — ONLY an
// explicit INT_STATUS read clears the latch, so unrelated diagnostic reads
// of other registers can't silently eat a pending interrupt), INT_LEVEL=0
// (active HIGH), INT_OPEN=0 (push-pull) -> 0x20 (same on both chip variants)
#define MPU_INT_PIN_CFG_VALUE  0x20
// INT_ENABLE (0x38): bit6 = MOT_EN on MPU6050 / WOM_EN on MPU6500 — same bit
// position on both chips -> 0x40
#define MPU_INT_ENABLE_MOTION  0x40

// ---- True MPU6050 (WHO_AM_I == 0x68) legacy motion-comparator tuning ----
// ACCEL_CONFIG (0x1C): AFS_SEL=0 (+/-2g), ACCEL_HPF=1 (DHPF ~5Hz, required
// for the hardware motion-detect comparator to work) -> 0x01
#define MPU_ACCEL_CONFIG_VALUE 0x01
// MOT_THR/MOT_DUR: 1 LSB of MOT_THR = 32 mg, 1 LSB of MOT_DUR = 1 ms.
#define MOTION_THRESHOLD    8       // -> ~256 mg trigger
#define MOTION_DURATION     20      // -> 20 ms sustained motion
// MOT_DETECT_CTRL (0x69): ACCEL_ON_DELAY=01, FF_COUNT decrement=01,
// MOT_COUNT decrement=01 -> 0x15. UNDOCUMENTED IN THE OFFICIAL DATASHEET BUT
// REQUIRED — leaving it at its power-on-reset value is a well-known reason
// the legacy motion comparator never trips even though ACCEL_CONFIG/MOT_THR/
// MOT_DUR/INT_ENABLE are all correct.
#define MPU_MOT_DETECT_CTRL_VALUE 0x15

// ---- MPU6500/9250 (WHO_AM_I == 0x70) Wake-on-Motion tuning ----
// WOM_THR (0x1F, same address as MOT_THR): 1 LSB = 4 mg.
#define MOTION_THRESHOLD_WOM   20     // -> ~80 mg trigger (tune to taste)
// LP_ACCEL_ODR (0x1E): low-power accel sample rate while cycling. 0x07 ==
// ~31.25 Hz — responsive enough to catch a bump/shake without much power.
#define MPU_LP_ACCEL_ODR_VALUE 0x07
// ACCEL_INTEL_CTRL (0x69, same address as MOT_DETECT_CTRL): bit7
// ACCEL_INTEL_EN=1, bit6 ACCEL_INTEL_MODE=1 (compare to previous sample)
// -> 0xC0. This is what actually arms the MPU6500's hardware motion engine —
// without it, WOM_THR/LP_ACCEL_ODR/INT_ENABLE are all configured but nothing
// ever compares against them.
#define MPU_ACCEL_INTEL_CTRL_VALUE 0xC0
// ACCEL_CONFIG for the MPU6500 path: +/-2g, no self-test, no HPF bits (the
// MPU6500 doesn't use ACCEL_CONFIG bits[2:0] for a software HPF the way the
// true MPU6050 does; motion detection lives entirely in ACCEL_INTEL_CTRL).
#define MPU_ACCEL_CONFIG_VALUE_6500 0x00
// PWR_MGMT_1 CYCLE bit (bit5): puts the chip into accelerometer-only
// low-power cycling at the LP_ACCEL_ODR rate — this is what the MPU6500's
// Wake-on-Motion feature requires to actually run its comparator.
#define MPU_PWR_MGMT1_CYCLE_VALUE 0x20
// PWR_MGMT_2: put all three gyro axes in standby while cycling on accel
// only, since WOM doesn't need the gyro running (saves power).
#define MPU_PWR_MGMT2_GYRO_STANDBY 0x07

// ---------------------------------------------------------------------------
// Radio / node configuration
// ---------------------------------------------------------------------------
#define LORA_FREQUENCY      433E6   // SX1278 433 MHz variant — South Africa ICASA SRD band
#define LORA_TX_POWER_DBM   14      // Conservative default — adjust to local regs
#define LORA_SPREADING_FACTOR 9
#define LORA_SIGNAL_BANDWIDTH  125E3
#define LORA_CODING_RATE4   5

#define NODE_ID             1001UL  // Unique per vehicle — change per deployment

// (Motion threshold/duration constants are defined above, in the MPU6050
// register-map section, since they differ by detected chip variant.)

// Wake/report timing
#define GPS_FIX_TIMEOUT_MS       45000UL   // Max time to wait for a GPS fix per wake
// DEMO_MODE 1 = 1-minute heartbeat, so the dashboard updates often and the
// gateway's loss-of-contact alert can use a short timeout while filming.
// Set back to 0 for normal operation (5-minute heartbeat, longer battery life).
// Keep the gateway's contact_timeout_minutes ABOVE 3 x the heartbeat, or a
// parked node will trigger false "contact lost" alerts between heartbeats.
#define DEMO_MODE 1
#if DEMO_MODE
#define HEARTBEAT_INTERVAL_US    (1ULL * 60ULL * 1000000ULL) // 1-minute report (demo)
#else
#define HEARTBEAT_INTERVAL_US    (5ULL * 60ULL * 1000000ULL) // 5-minute routine report
#endif

// Battery monitoring (resistive divider into an ADC1 pin). Read with
// analogReadMilliVolts(), which applies the ESP32's factory eFuse ADC
// calibration — the raw analogRead()/4095*3300 formula is off by up to
// ~10% on the ESP32 because its ADC is non-linear, especially near the rails.
// Set to 1 ONLY once the divider is actually wired (battery+ -> 100k -> GPIO34
// -> 100k -> GND). With nothing connected, GPIO34 floats and returns random
// noise, so while this is 0 the node reports 0 mV = "not measured" and the
// gateway/dashboard show no battery value instead of a fake one.
#define BATTERY_MONITOR_ENABLED 0
#define BATTERY_ADC_PIN     34
#define BATTERY_DIVIDER_RATIO 2.0f   // two equal resistors halving VBAT
#define BATTERY_ADC_SAMPLES   8      // averaged to smooth ADC noise

// Reliability / retry tuning
#define MPU_INIT_RETRIES     3
#define MPU_INIT_RETRY_DELAY_MS  100
#define LORA_INIT_RETRIES    3
#define LORA_INIT_RETRY_DELAY_MS 200

// ---------------------------------------------------------------------------
// BRING-UP DIAGNOSTIC — keep at 0 for normal operation and demos. When set to
// 1 it polls the MPU6050's interrupt/motion status registers and the raw
// INT_PIN level for MPU_SELFTEST_WINDOW_MS on EVERY boot, which delays every
// wake (including a theft wake) by that long before anything is transmitted.
// Only enable it temporarily while debugging the motion-interrupt wiring.
// ---------------------------------------------------------------------------
#define ENABLE_MPU_MOTION_SELFTEST 0
#define MPU_SELFTEST_WINDOW_MS     15000UL

// ---------------------------------------------------------------------------
// Telemetry packet v2 — fixed-size binary layout shared with the RPi5 gateway
// (pi_gateway.py TELEMETRY_V2_FORMAT = "<BBIIffffBBBH", 31 bytes).
// ---------------------------------------------------------------------------
#define PACKET_PROTOCOL_VERSION   2

#define PACKET_TYPE_HEARTBEAT       0   // periodic timer wake
#define PACKET_TYPE_MOTION_ALERT    1   // sent immediately on motion wake, BEFORE GPS
#define PACKET_TYPE_MOTION_UPDATE   2   // sent after the GPS attempt on a motion wake

typedef struct __attribute__((packed)) {
  uint8_t  protocolVersion; // PACKET_PROTOCOL_VERSION
  uint8_t  packetType;      // PACKET_TYPE_*
  uint32_t nodeId;
  uint32_t sequence;        // increments on every transmission (RTC memory)
  float    latitude;
  float    longitude;
  float    speedKmph;
  float    headingDeg;
  uint8_t  satellites;
  uint8_t  motionFlag;      // 1 = this wake was caused by the motion interrupt
  uint8_t  gpsValid;        // 1 = fresh fix this cycle, 0 = stale/cached fix
  uint16_t batteryMilliVolts;
} TelemetryPacket;

static_assert(sizeof(TelemetryPacket) == 31, "TelemetryPacket must be 31 bytes to match the gateway");

// ---------------------------------------------------------------------------
// State persisted across deep-sleep cycles (RTC memory). Survives deep sleep,
// resets on power loss / hard reset — the gateway treats a sequence number
// that jumps back to a small value as a node restart.
// ---------------------------------------------------------------------------
RTC_DATA_ATTR float    lastLat = 0.0f;
RTC_DATA_ATTR float    lastLng = 0.0f;
RTC_DATA_ATTR uint8_t  lastSats = 0;
RTC_DATA_ATTR bool     hasEverFixed = false;
RTC_DATA_ATTR uint32_t bootCount = 0;
RTC_DATA_ATTR uint32_t packetSequence = 0;

TinyGPSPlus gps;
HardwareSerial GPSSerial(2); // UART2

// Detected at boot from WHO_AM_I — selects which motion-interrupt register
// scheme configureMpuMotionInterrupt()/dumpMpuMotionConfig() use.
bool mpuIsVariant6500 = false;

// ---------------------------------------------------------------------------
// Forward declarations
// ---------------------------------------------------------------------------
bool     mpuWriteRegister(uint8_t reg, uint8_t value);
int16_t  mpuReadRegister16(uint8_t reg);
bool     mpuReadRegister8(uint8_t reg, uint8_t *outValue);
bool     mpuPingI2C();
bool     initializeMpuWithRetries();
void     configureMpuMotionInterrupt();
void     clearMpuInterruptLatch();
void     runMpuMotionSelfTest(unsigned long windowMs);
void     dumpMpuMotionConfig();
bool     acquireGpsFix(unsigned long timeoutMs);
uint16_t readBatteryMilliVolts();
bool     initializeLoraWithRetries();
void     sendTelemetry(uint8_t packetType, bool motionTriggered, bool gpsFixValid);
void     goToDeepSleep(bool armMotionWake);
const char *wakeReasonToString(esp_sleep_wakeup_cause_t cause);

// ===========================================================================
void setup() {
  Serial.begin(115200);
  delay(50);

  bootCount++;
  esp_sleep_wakeup_cause_t wakeCause = esp_sleep_get_wakeup_cause();

  Serial.println();
  Serial.printf("[BOOT #%lu] Wake reason: %s\n", (unsigned long)bootCount, wakeReasonToString(wakeCause));

  // --- I2C bus ---
  if (!Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN)) {
    Serial.println("[ERROR] I2C bus (Wire) failed to initialize on SDA=21/SCL=22.");
  }
  Wire.setClock(400000);

  // --- MPU6050 bring-up via direct register access, with retries ---
  bool mpuReady = initializeMpuWithRetries();

  if (mpuReady) {
    configureMpuMotionInterrupt();
    clearMpuInterruptLatch();   // clear any pending latch left over from before sleep
    Serial.println("[MPU6050] Ready — motion interrupt armed.");
    dumpMpuMotionConfig();

    // Diagnostic snapshot of raw accel data for this boot (debug only —
    // not part of the telemetry packet).
    int16_t ax = mpuReadRegister16(MPU_REG_ACCEL_XOUT_H);
    int16_t ay = mpuReadRegister16(MPU_REG_ACCEL_XOUT_H + 2);
    int16_t az = mpuReadRegister16(MPU_REG_ACCEL_XOUT_H + 4);
    Serial.printf("[MPU6050] Raw accel snapshot: X=%d Y=%d Z=%d\n", ax, ay, az);

#if ENABLE_MPU_MOTION_SELFTEST
    runMpuMotionSelfTest(MPU_SELFTEST_WINDOW_MS);
#endif
  } else {
    Serial.println("[CRITICAL] MPU6050 unavailable after retries — motion-wake DISABLED this boot; "
                    "falling back to timer-only heartbeat reporting.");
  }

  bool motionTriggered = mpuReady && (wakeCause == ESP_SLEEP_WAKEUP_EXT0);

  // --- LoRa init FIRST (with retries), so a motion wake can be reported
  //     immediately instead of after the GPS timeout ---
  SPI.begin(LORA_SCK_PIN, LORA_MISO_PIN, LORA_MOSI_PIN, LORA_NSS_PIN);
  LoRa.setPins(LORA_NSS_PIN, LORA_RST_PIN, LORA_DIO0_PIN);

  bool loraReady = initializeLoraWithRetries();

  if (loraReady) {
    LoRa.setSpreadingFactor(LORA_SPREADING_FACTOR);
    LoRa.setSignalBandwidth(LORA_SIGNAL_BANDWIDTH);
    LoRa.setCodingRate4(LORA_CODING_RATE4);
    LoRa.setTxPower(LORA_TX_POWER_DBM);
    LoRa.enableCrc();

    if (motionTriggered) {
      // Immediate motion report with the last cached position (gpsValid=0).
      sendTelemetry(PACKET_TYPE_MOTION_ALERT, true, false);
    }
    LoRa.sleep();   // ~0.2 uA instead of ~1.6 mA standby while we wait for GPS
  } else {
    Serial.println("[CRITICAL] LoRa radio unavailable after retries — telemetry NOT sent this cycle. "
                    "Will retry fresh on next wake.");
  }

  // --- GPS acquisition ---
  GPSSerial.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
  bool gotFix = acquireGpsFix(GPS_FIX_TIMEOUT_MS);

  if (loraReady) {
    // beginPacket() inside sendTelemetry() wakes the radio from sleep itself.
    sendTelemetry(motionTriggered ? PACKET_TYPE_MOTION_UPDATE : PACKET_TYPE_HEARTBEAT,
                  motionTriggered, gotFix);
    LoRa.sleep();   // keep the SX1278 in sleep for the whole ESP32 deep sleep
  }

  goToDeepSleep(mpuReady);
}

void loop() {
  // Never reached — device sleeps at the end of setup() every cycle.
}

// ===========================================================================
// Low-level MPU6050 I2C helpers (no external MPU6050 library — Wire.h only)
// ===========================================================================

// Write a single byte to an MPU6050 register. Returns true on I2C ACK.
bool mpuWriteRegister(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(MPU6050_I2C_ADDR);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

// Read a single byte from an MPU6050 register. Returns true on success and
// stores the value in *outValue.
bool mpuReadRegister8(uint8_t reg, uint8_t *outValue) {
  Wire.beginTransmission(MPU6050_I2C_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    return false;
  }

  if (Wire.requestFrom((uint8_t)MPU6050_I2C_ADDR, (uint8_t)1) != 1) {
    return false;
  }

  *outValue = Wire.read();
  return true;
}

// Read a big-endian 16-bit word starting at the given register (used for the
// ACCEL_*OUT_H / GYRO_*OUT_H pairs). Returns 0 if the bus read fails.
int16_t mpuReadRegister16(uint8_t reg) {
  Wire.beginTransmission(MPU6050_I2C_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    return 0;
  }

  if (Wire.requestFrom((uint8_t)MPU6050_I2C_ADDR, (uint8_t)2) != 2) {
    return 0;
  }

  int16_t high = Wire.read();
  int16_t low  = Wire.read();
  return (high << 8) | low;
}

// Bench-verified presence check: a bare I2C address ping (no register access)
// is enough to confirm the module ACKs on the bus.
bool mpuPingI2C() {
  Wire.beginTransmission(MPU6050_I2C_ADDR);
  return Wire.endTransmission() == 0;
}

// ===========================================================================
// MPU6050 bring-up with bounded retries. Pings the device, then wakes it out
// of its power-on-reset sleep state (PWR_MGMT_1 = 0x00). Returns true only if
// both the ping and the wake write succeed.
// ===========================================================================
bool initializeMpuWithRetries() {
  for (uint8_t attempt = 1; attempt <= MPU_INIT_RETRIES; attempt++) {
    if (mpuPingI2C()) {
      if (mpuWriteRegister(MPU_REG_PWR_MGMT_1, 0x00)) {
        delay(20);  // allow the clock/PLL to settle after waking
        return true;
      }
      Serial.printf("[ERROR] MPU6050 ACKed but wake write (PWR_MGMT_1) failed (attempt %u/%u).\n",
                    attempt, MPU_INIT_RETRIES);
    } else {
      Serial.printf("[ERROR] MPU6050 not responding at 0x68 (attempt %u/%u) — check wiring.\n",
                    attempt, MPU_INIT_RETRIES);
    }

    if (attempt < MPU_INIT_RETRIES) {
      delay(MPU_INIT_RETRY_DELAY_MS);
    }
  }
  return false;
}

// ===========================================================================
// Motion-interrupt configuration via direct register writes. Branches on the
// chip variant detected from WHO_AM_I (see header comment for why this
// matters — true MPU6050 and MPU6500 use incompatible motion-interrupt
// register schemes despite sharing the same I2C address and accel/gyro
// output registers).
// ===========================================================================
void configureMpuMotionInterrupt() {
  uint8_t whoAmI = 0;
  mpuReadRegister8(MPU_REG_WHO_AM_I, &whoAmI);
  mpuIsVariant6500 = (whoAmI == MPU_WHOAMI_MPU6500);

  Serial.printf("[MPU6050] WHO_AM_I=0x%02X -> configuring as %s\n",
                whoAmI, mpuIsVariant6500 ? "MPU6500 (Wake-on-Motion)" : "MPU6050 (legacy motion comparator)");

  if (!mpuWriteRegister(MPU_REG_SIGNAL_PATH_RESET, 0x07)) {
    Serial.println("[WARN] SIGNAL_PATH_RESET write failed.");
  }
  delay(20);  // let the reset signal paths settle before reconfiguring

  if (!mpuWriteRegister(MPU_REG_INT_PIN_CFG, MPU_INT_PIN_CFG_VALUE)) {
    Serial.println("[WARN] INT_PIN_CFG write failed.");
  }

  if (mpuIsVariant6500) {
    // ---- MPU6500/9250 Wake-on-Motion sequence ----
    if (!mpuWriteRegister(MPU_REG_ACCEL_CONFIG, MPU_ACCEL_CONFIG_VALUE_6500)) {
      Serial.println("[WARN] ACCEL_CONFIG write failed.");
    }
    if (!mpuWriteRegister(MPU_REG_MOT_THR, MOTION_THRESHOLD_WOM)) {   // WOM_THR
      Serial.println("[WARN] WOM_THR write failed.");
    }
    if (!mpuWriteRegister(MPU_REG_LP_ACCEL_ODR, MPU_LP_ACCEL_ODR_VALUE)) {
      Serial.println("[WARN] LP_ACCEL_ODR write failed.");
    }
    if (!mpuWriteRegister(MPU_REG_MOT_DETECT_CTRL, MPU_ACCEL_INTEL_CTRL_VALUE)) {  // ACCEL_INTEL_CTRL
      Serial.println("[WARN] ACCEL_INTEL_CTRL write failed.");
    }
    if (!mpuWriteRegister(MPU_REG_INT_ENABLE, MPU_INT_ENABLE_MOTION)) {
      Serial.println("[WARN] INT_ENABLE write failed.");
    }
    if (!mpuWriteRegister(MPU_REG_PWR_MGMT_2, MPU_PWR_MGMT2_GYRO_STANDBY)) {
      Serial.println("[WARN] PWR_MGMT_2 (gyro standby) write failed.");
    }
    // CYCLE mode written LAST — this is what actually starts the low-power
    // accelerometer sampling that the Wake-on-Motion comparator runs against.
    if (!mpuWriteRegister(MPU_REG_PWR_MGMT_1, MPU_PWR_MGMT1_CYCLE_VALUE)) {
      Serial.println("[WARN] PWR_MGMT_1 (CYCLE) write failed.");
    }
  } else {
    // ---- True MPU6050 legacy motion-comparator sequence ----
    if (!mpuWriteRegister(MPU_REG_ACCEL_CONFIG, MPU_ACCEL_CONFIG_VALUE)) {
      Serial.println("[WARN] ACCEL_CONFIG write failed.");
    }
    if (!mpuWriteRegister(MPU_REG_MOT_THR, MOTION_THRESHOLD)) {
      Serial.println("[WARN] MOT_THR write failed.");
    }
    if (!mpuWriteRegister(MPU_REG_MOT_DUR, MOTION_DURATION)) {
      Serial.println("[WARN] MOT_DUR write failed.");
    }
    if (!mpuWriteRegister(MPU_REG_MOT_DETECT_CTRL, MPU_MOT_DETECT_CTRL_VALUE)) {
      Serial.println("[WARN] MOT_DETECT_CTRL write failed.");
    }
    if (!mpuWriteRegister(MPU_REG_INT_ENABLE, MPU_INT_ENABLE_MOTION)) {
      Serial.println("[WARN] INT_ENABLE write failed.");
    }
  }

  pinMode(MPU_INT_PIN, INPUT);
}

// ===========================================================================
// Read back every register configureMpuMotionInterrupt() just wrote, plus
// WHO_AM_I, and print them. This proves whether the writes actually stuck
// (a returned I2C ACK only confirms the bus transaction completed — it does
// NOT prove the device latched the value), and confirms which chip variant
// was detected (0x68 = true MPU6050, 0x70 = MPU6500).
// ===========================================================================
void dumpMpuMotionConfig() {
  uint8_t whoAmI = 0, intPinCfg = 0, accelCfg = 0, thr = 0, intEnable = 0;

  mpuReadRegister8(MPU_REG_WHO_AM_I, &whoAmI);
  mpuReadRegister8(MPU_REG_INT_PIN_CFG, &intPinCfg);
  mpuReadRegister8(MPU_REG_ACCEL_CONFIG, &accelCfg);
  mpuReadRegister8(MPU_REG_MOT_THR, &thr);   // MOT_THR or WOM_THR depending on variant
  mpuReadRegister8(MPU_REG_INT_ENABLE, &intEnable);

  Serial.println("[MPU6050 CONFIG READBACK]");
  Serial.printf("  WHO_AM_I        = 0x%02X  (0x68=MPU6050, 0x70=MPU6500)\n", whoAmI);
  Serial.printf("  INT_PIN_CFG     = 0x%02X  (expect 0x%02X)\n", intPinCfg, MPU_INT_PIN_CFG_VALUE);
  Serial.printf("  INT_ENABLE      = 0x%02X  (expect 0x%02X)\n", intEnable, MPU_INT_ENABLE_MOTION);

  bool mismatch = (intPinCfg != MPU_INT_PIN_CFG_VALUE) || (intEnable != MPU_INT_ENABLE_MOTION);

  if (mpuIsVariant6500) {
    uint8_t lpOdr = 0, intelCtrl = 0, pwrMgmt1 = 0, pwrMgmt2 = 0;
    mpuReadRegister8(MPU_REG_LP_ACCEL_ODR, &lpOdr);
    mpuReadRegister8(MPU_REG_MOT_DETECT_CTRL, &intelCtrl);   // ACCEL_INTEL_CTRL
    mpuReadRegister8(MPU_REG_PWR_MGMT_1, &pwrMgmt1);
    mpuReadRegister8(MPU_REG_PWR_MGMT_2, &pwrMgmt2);

    Serial.printf("  ACCEL_CONFIG    = 0x%02X  (expect 0x%02X)\n", accelCfg, MPU_ACCEL_CONFIG_VALUE_6500);
    Serial.printf("  WOM_THR         = 0x%02X  (expect 0x%02X)\n", thr, MOTION_THRESHOLD_WOM);
    Serial.printf("  LP_ACCEL_ODR    = 0x%02X  (expect 0x%02X)\n", lpOdr, MPU_LP_ACCEL_ODR_VALUE);
    Serial.printf("  ACCEL_INTEL_CTRL= 0x%02X  (expect 0x%02X)\n", intelCtrl, MPU_ACCEL_INTEL_CTRL_VALUE);
    Serial.printf("  PWR_MGMT_1      = 0x%02X  (expect 0x%02X, CYCLE bit)\n", pwrMgmt1, MPU_PWR_MGMT1_CYCLE_VALUE);
    Serial.printf("  PWR_MGMT_2      = 0x%02X  (expect 0x%02X, gyro standby)\n", pwrMgmt2, MPU_PWR_MGMT2_GYRO_STANDBY);

    mismatch = mismatch || (accelCfg != MPU_ACCEL_CONFIG_VALUE_6500) || (thr != MOTION_THRESHOLD_WOM) ||
               (lpOdr != MPU_LP_ACCEL_ODR_VALUE) || (intelCtrl != MPU_ACCEL_INTEL_CTRL_VALUE) ||
               (pwrMgmt1 != MPU_PWR_MGMT1_CYCLE_VALUE) || (pwrMgmt2 != MPU_PWR_MGMT2_GYRO_STANDBY);
  } else {
    uint8_t dur = 0, ctrl = 0;
    mpuReadRegister8(MPU_REG_MOT_DUR, &dur);
    mpuReadRegister8(MPU_REG_MOT_DETECT_CTRL, &ctrl);

    Serial.printf("  ACCEL_CONFIG    = 0x%02X  (expect 0x%02X)\n", accelCfg, MPU_ACCEL_CONFIG_VALUE);
    Serial.printf("  MOT_THR         = 0x%02X  (expect 0x%02X)\n", thr, MOTION_THRESHOLD);
    Serial.printf("  MOT_DUR         = 0x%02X  (expect 0x%02X)\n", dur, MOTION_DURATION);
    Serial.printf("  MOT_DETECT_CTRL = 0x%02X  (expect 0x%02X)\n", ctrl, MPU_MOT_DETECT_CTRL_VALUE);

    mismatch = mismatch || (accelCfg != MPU_ACCEL_CONFIG_VALUE) || (thr != MOTION_THRESHOLD) ||
               (dur != MOTION_DURATION) || (ctrl != MPU_MOT_DETECT_CTRL_VALUE);
  }

  if (mismatch) {
    Serial.println("[WARN] One or more registers did NOT read back as written — the I2C bus is");
    Serial.println("  ACKing but not reliably latching data (check bus speed, wire length/quality,");
    Serial.println("  or add/verify pull-up resistors on SDA/SCL).");
  }
}

// ===========================================================================
// Clear the MPU6050 interrupt latch by reading INT_STATUS. INT_PIN_CFG sets
// INT_RD_CLEAR=0, so ONLY a read of INT_STATUS clears the latch — other
// register reads (e.g. the config readback) cannot silently eat a pending
// motion interrupt.
// ===========================================================================
void clearMpuInterruptLatch() {
  uint8_t status = 0;
  if (!mpuReadRegister8(MPU_REG_INT_STATUS, &status)) {
    Serial.println("[WARN] Failed to read/clear MPU6050 INT_STATUS.");
  }
}

// ===========================================================================
// TEMPORARY DIAGNOSTIC: polls the MPU6050's interrupt/motion status and the
// raw INT_PIN level for windowMs, printing every observation of a tripped
// motion bit. Shake/tap the board while this is running. Remove or set
// ENABLE_MPU_MOTION_SELFTEST to 0 once the hardware wake path is confirmed.
// ===========================================================================
void runMpuMotionSelfTest(unsigned long windowMs) {
  Serial.println();
  Serial.printf("[MPU6050 SELF-TEST] Shake/tap the board now — polling for %lums...\n", windowMs);

  unsigned long start = millis();
  bool motionSeen = false;
  uint32_t pollCount = 0;

  while (millis() - start < windowMs) {
    uint8_t intStatus = 0;
    uint8_t motStatus = 0;
    bool intOk = mpuReadRegister8(MPU_REG_INT_STATUS, &intStatus);
    bool motOk = mpuReadRegister8(MPU_REG_MOT_DETECT_STATUS, &motStatus);
    int pinLevel = digitalRead(MPU_INT_PIN);

    bool motionBitSet = intOk && (intStatus & 0x40);   // INT_STATUS bit6 = MOT

    if (motionBitSet || (motOk && motStatus != 0) || pinLevel == HIGH) {
      motionSeen = true;
      Serial.printf("[MPU6050 SELF-TEST] EVENT — INT_STATUS=0x%02X MOT_DETECT_STATUS=0x%02X INT_PIN=%d\n",
                    intOk ? intStatus : 0xFF, motOk ? motStatus : 0xFF, pinLevel);
    }

    // Every ~2s, print a raw accel snapshot so you can see live deltas even
    // if the hardware comparator never trips.
    if ((pollCount % 40) == 0) {
      int16_t ax = mpuReadRegister16(MPU_REG_ACCEL_XOUT_H);
      int16_t ay = mpuReadRegister16(MPU_REG_ACCEL_XOUT_H + 2);
      int16_t az = mpuReadRegister16(MPU_REG_ACCEL_XOUT_H + 4);
      Serial.printf("[MPU6050 SELF-TEST] accel X=%d Y=%d Z=%d | INT_PIN=%d\n", ax, ay, az, pinLevel);
    }

    pollCount++;
    delay(50);
  }

  if (motionSeen) {
    Serial.println("[MPU6050 SELF-TEST] PASS — motion bit and/or INT_PIN went active during the window.");
  } else {
    Serial.println("[MPU6050 SELF-TEST] FAIL — no motion event observed. Likely causes: MOT_THR set too");
    Serial.println("  high for the shake force used, INT line not actually wired to GPIO27, or this");
    Serial.println("  MPU6050 unit's legacy motion comparator not responding to these registers.");
  }

  clearMpuInterruptLatch();
  Serial.println();
}

// ===========================================================================
// GPS: block (with timeout) until a valid fix, feeding TinyGPSPlus.
// Falls back to the last known fix cached in RTC memory if it times out.
// ===========================================================================
bool acquireGpsFix(unsigned long timeoutMs) {
  unsigned long start = millis();

  while (millis() - start < timeoutMs) {
    while (GPSSerial.available() > 0) {
      gps.encode(GPSSerial.read());
    }

    if (gps.location.isValid() && gps.location.isUpdated()) {
      lastLat = gps.location.lat();
      lastLng = gps.location.lng();
      lastSats = (uint8_t)gps.satellites.value();
      hasEverFixed = true;
      Serial.printf("[GPS] Fix acquired: %.6f, %.6f (sats=%u) in %lums\n",
                    lastLat, lastLng, lastSats, millis() - start);
      return true;
    }
    delay(50);
  }

  Serial.println("[GPS] Timed out waiting for fix — using last cached position (if any).");
  return false;
}

// ===========================================================================
// Battery voltage via resistive divider on BATTERY_ADC_PIN
// ===========================================================================
uint16_t readBatteryMilliVolts() {
#if !BATTERY_MONITOR_ENABLED
  return 0;   // 0 = not measured (no divider wired)
#endif
  uint32_t sumPinMilliVolts = 0;
  for (uint8_t i = 0; i < BATTERY_ADC_SAMPLES; i++) {
    sumPinMilliVolts += analogReadMilliVolts(BATTERY_ADC_PIN);   // eFuse-calibrated
    delay(2);
  }
  float pinMilliVolts = (float)sumPinMilliVolts / BATTERY_ADC_SAMPLES;
  float batteryMilliVolts = pinMilliVolts * BATTERY_DIVIDER_RATIO;
  if (batteryMilliVolts > 65535.0f) batteryMilliVolts = 65535.0f;
  return (uint16_t)batteryMilliVolts;
}

// ===========================================================================
// LoRa bring-up with bounded retries. Returns true once LoRa.begin() succeeds,
// false if the radio never answers within LORA_INIT_RETRIES tries.
// ===========================================================================
bool initializeLoraWithRetries() {
  for (uint8_t attempt = 1; attempt <= LORA_INIT_RETRIES; attempt++) {
    if (LoRa.begin(LORA_FREQUENCY)) {
      return true;
    }

    Serial.printf("[ERROR] LoRa radio init failed (attempt %u/%u) — check SPI wiring/module.\n",
                  attempt, LORA_INIT_RETRIES);

    if (attempt < LORA_INIT_RETRIES) {
      delay(LORA_INIT_RETRY_DELAY_MS);
    }
  }
  return false;
}

// ===========================================================================
// Build and transmit the telemetry packet over LoRa
// ===========================================================================
void sendTelemetry(uint8_t packetType, bool motionTriggered, bool gpsFixValid) {
  packetSequence++;

  TelemetryPacket packet;
  packet.protocolVersion  = PACKET_PROTOCOL_VERSION;
  packet.packetType       = packetType;
  packet.nodeId           = NODE_ID;
  packet.sequence         = packetSequence;
  packet.latitude         = hasEverFixed ? lastLat : 0.0f;
  packet.longitude        = hasEverFixed ? lastLng : 0.0f;
  // Speed/heading only mean something alongside a fresh fix from THIS wake.
  packet.speedKmph        = (gpsFixValid && gps.speed.isValid()) ? (float)gps.speed.kmph() : 0.0f;
  packet.headingDeg       = (gpsFixValid && gps.course.isValid()) ? (float)gps.course.deg() : 0.0f;
  packet.satellites       = lastSats;
  packet.motionFlag       = motionTriggered ? 1 : 0;
  packet.gpsValid         = gpsFixValid ? 1 : 0;
  packet.batteryMilliVolts = readBatteryMilliVolts();

  LoRa.beginPacket();
  LoRa.write((uint8_t *)&packet, sizeof(TelemetryPacket));
  bool sent = LoRa.endPacket();   // blocking; returns once TX is done

  Serial.printf("[LORA] TX %s type=%u seq=%lu node=%lu motion=%u gpsValid=%u lat=%.6f lng=%.6f "
                "speed=%.1f batt=%umV (%u bytes)\n",
                sent ? "OK" : "FAILED", packet.packetType, (unsigned long)packet.sequence,
                (unsigned long)packet.nodeId, packet.motionFlag, packet.gpsValid,
                packet.latitude, packet.longitude, packet.speedKmph,
                packet.batteryMilliVolts, (unsigned)sizeof(TelemetryPacket));
}

// ===========================================================================
// Arm wake source(s) and enter deep sleep.
// armMotionWake is false when the MPU6050 failed to initialize this boot —
// in that case only the timer wakeup is armed, since leaving ext0 armed
// against a non-responsive/uninitialized sensor's INT line risks a floating
// pin and spurious immediate wakeups (draining the battery in a wake loop).
// ===========================================================================
void goToDeepSleep(bool armMotionWake) {
  if (armMotionWake) {
    // Ensure the MPU6050 interrupt latch is cleared right before sleeping so
    // ext0's level-triggered wake doesn't fire immediately on a stale flag.
    clearMpuInterruptLatch();
    esp_sleep_enable_ext0_wakeup((gpio_num_t)MPU_INT_PIN, HIGH);
  }

  esp_sleep_enable_timer_wakeup(HEARTBEAT_INTERVAL_US);

  Serial.printf("[SLEEP] Entering deep sleep (%s + %llu-min heartbeat armed, LoRa asleep).\n",
                armMotionWake ? "motion interrupt" : "motion interrupt DISABLED — MPU6050 fault",
                HEARTBEAT_INTERVAL_US / 60000000ULL);
  Serial.flush();

  esp_deep_sleep_start();
}

// ===========================================================================
const char *wakeReasonToString(esp_sleep_wakeup_cause_t cause) {
  switch (cause) {
    case ESP_SLEEP_WAKEUP_EXT0:     return "EXT0 (MPU6050 motion interrupt)";
    case ESP_SLEEP_WAKEUP_TIMER:    return "TIMER (periodic heartbeat)";
    case ESP_SLEEP_WAKEUP_UNDEFINED:
    default:                        return "UNDEFINED (power-on / reset)";
  }
}
