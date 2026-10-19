// =================================================================
// Stage 2: Supreme AXP2101 / Both I2C Buses / Shutdown Test
// =================================================================
// Purpose: Record boot rails before changing them, safely power the GPS,
// and repeat #106's shutdown/VBUS power-on check.
// What to expect: a boot snapshot, ALDO3 OFF, ALDO4 at 3.3 V, bus scans,
// then status every 5 s. Keys: h help, p status, i scan, o sensors,
// d checks SD power/mount/write/read/delete, then unmounts and powers off SD.
// SD/IMU SPI pins below are from LilyGO, unverified until d passes on the bench.
// s then uppercase Y within 10 s to shut down (any other key cancels).
// Bench 2026-10-18: Wire1 SDA42/SCL41 = PMU 0x34 + PCF8563 RTC 0x51.
// Wire SDA17/SCL18 = SH1106 OLED 0x3C + BME280 0x77; unexplained ACKs
// at reserved 0x7C/0x7E were harmless. The RTC is on the PMU bus.
// Boot snapshot: DCDC1/5 3.3 V ON, DCDC4 1.8 V ON, ALDO1 1.8 V ON,
// ALDO2 3.3 V ON, BLDO1 1.8 V ON; ALDO4/VBACKUP and other rails OFF,
// except ALDO3: ON after VBUS insert, OFF after POWER-button power-on.
// PMU.shutdown() switched off and stayed off 15 s with USB connected.
// USB unplug/replug powered it on: ON source 0x04 (VBUS insert),
// OFF source 0x02 (software).
// Requires installed XPowersLib 0.3.3; do not substitute a different API.
//
// Supreme upload settings (bench-confirmed 2026-10-18):
//   Before ANY power, attach the 915 MHz LoRa antenna: factory firmware
//   may transmit. Back up the factory flash before the first upload.
//   Arduino IDE: ESP32S3 Dev Module, USB CDC On Boot Enabled,
//   USB Mode Hardware CDC and JTAG, Flash Size 8 MB, PSRAM QSPI PSRAM.
//   CLI FQBN (ESP32 core 3.3.11):
//   esp32:esp32:esp32s3:CDCOnBoot=cdc,USBMode=hwcdc,FlashSize=8M,PSRAM=enabled
//   Stage 1 printed flash 8388608 and PSRAM 8388608 bytes with QSPI.
//   Native USB-Serial/JTAG is VID:PID 303A:1001; the COM number varies.
//   With our sketches running, esptool auto-reset needs no buttons;
//   it failed under the factory Meshtastic firmware.
//   Manual download with an 18650 (USB unplug alone leaves it powered):
//   hold POWER ~6 s to switch off, hold BOOT, press POWER ~1 s, keep
//   BOOT held 2-3 s, then release. Without a battery: unplug USB, hold
//   BOOT, plug USB in, wait 2-3 s, then release BOOT.
//   After a manual-download flash, software/RTS reset returns to download
//   mode: the BOOT strap stays latched until a real reset. Press RESET
//   once to run the sketch.
//   Buttons (LilyGO photo, bench-confirmed): screen left, USB-C right.
//   RESET/POWER are a close pair near the middle, RESET on the left;
//   BOOT is on its own further right. Hold POWER ~6 s to switch off.
//
// Pin/rail source: Xinyuan-LilyGO/LilyGo-LoRa-Series, commit
// 1991b1ed70915d4f246b841f99f6027166ea7330, examples/Factory/utilities.h
// and LoRaBoards.cpp, T_BEAM_S3_SUPREME sections. Bench results above.
// Unlike LilyGO's factory setup, never enable ALDO3 (LoRa) here, and
// never configure/disable DCDC1 (ESP32) or other core rails. BLDO1 (SD)
// is held off after the snapshot except during the explicit d check.
// =================================================================

#include <Wire.h>
#include <XPowersLib.h>
#include <SPI.h>
#include <SD.h>
#include <ff.h>
#include <diskio.h>
#include <sd_diskio.h>

static const int PMU_SDA = 42, PMU_SCL = 41, PMU_IRQ = 40;
static const int SENSOR_SDA = 17, SENSOR_SCL = 18;
static const int SD_MOSI = 35, SD_SCK = 36, SD_MISO = 37, SD_CS = 47, IMU_CS = 34;
// Core SD default: ample for logging, conservative until the bus is bench-tested.
static constexpr uint32_t SD_SPI_HZ = 4000000;
SPIClass sdSpi(HSPI);  // independent of the radio's SPI instance
XPowersAXP2101 PMU;
bool shutdownPending = false;
uint32_t confirmStarted = 0;

static void printRail(const char* name, bool enabled, uint16_t millivolts) {
    // These are register setpoints, not measured output voltages.
    Serial.printf("  %-8s %-3s %u mV setpoint\n", name, enabled ? "ON" : "OFF", millivolts);
}

static void printStatus() {
    Serial.println("Rail enable states and programmed voltages (not measurements):");
    printRail("DCDC1", PMU.isEnableDC1(), PMU.getDC1Voltage());
    printRail("DCDC2", PMU.isEnableDC2(), PMU.getDC2Voltage());
    printRail("DCDC3", PMU.isEnableDC3(), PMU.getDC3Voltage());
    printRail("DCDC4", PMU.isEnableDC4(), PMU.getDC4Voltage());
    printRail("DCDC5", PMU.isEnableDC5(), PMU.getDC5Voltage());
    printRail("ALDO1", PMU.isEnableALDO1(), PMU.getALDO1Voltage());
    printRail("ALDO2", PMU.isEnableALDO2(), PMU.getALDO2Voltage());
    printRail("ALDO3", PMU.isEnableALDO3(), PMU.getALDO3Voltage());
    printRail("ALDO4", PMU.isEnableALDO4(), PMU.getALDO4Voltage());
    printRail("BLDO1", PMU.isEnableBLDO1(), PMU.getBLDO1Voltage());
    printRail("BLDO2", PMU.isEnableBLDO2(), PMU.getBLDO2Voltage());
    printRail("DLDO1", PMU.isEnableDLDO1(), PMU.getDLDO1Voltage());
    printRail("DLDO2", PMU.isEnableDLDO2(), PMU.getDLDO2Voltage());
    printRail("CPUSLDO", PMU.isEnableCPUSLDO(), PMU.getCPUSLDOVoltage());
    printRail("VBACKUP", PMU.isEnableButtonBatteryCharge(), PMU.getButtonBatteryVoltage());
    Serial.println("  VBACKUP = button-battery charge enable/target; GPS backup uses the 18650, not VBACKUP.");
    Serial.printf("VBUS present=%s, %u mV | battery present=%s, %u mV, %d%%\n",
                  PMU.isVbusIn() ? "yes" : "no", PMU.getVbusVoltage(),
                  PMU.isBatteryConnect() ? "yes" : "no", PMU.getBattVoltage(),
                  PMU.getBatteryPercent());
    Serial.printf("Charging=%s (state %d) | system=%u mV | PMU IRQ GPIO %d=%d\n",
                  PMU.isCharging() ? "yes" : "no", (int)PMU.getChargerStatus(),
                  PMU.getSystemVoltage(), PMU_IRQ, digitalRead(PMU_IRQ));
    Serial.println("  Charge states: 0 trickle, 1 precharge, 2 constant current, 3 constant voltage, 4 done, 5 stopped.");
    // XPowersLib returns the raw bitmask, not a single enumerated cause.
    int on = (int)PMU.getPowerOnSource(), off = (int)PMU.getPowerOffSource();
    Serial.printf("Power-on source=0x%02X | power-off source=0x%02X\n", on, off);
    Serial.println("  ON bits: 5 POWERON high, 4 battery insert, 3 battery charged >3.3 V,");
    Serial.println("           2 VBUS insert, 1 IRQ low, 0 POWERON low; zero = unknown.");
    Serial.println("  OFF bits: 7 overtemp, 6 DC overvolt, 5 DC undervolt, 4 VBUS overvolt,");
    Serial.println("            3 system undervolt, 2 POWERON held low, 1 software, 0 POWERON pull-down.");
}

static void scanBus(TwoWire& bus, const char* name) {
    Serial.printf("I2C scan: %s\n", name);
    unsigned found = 0;
    for (uint8_t address = 1; address < 127; ++address) {
        bus.beginTransmission(address);
        uint8_t result = bus.endTransmission();
        if (result == 0) {
            Serial.printf("  ACK at 0x%02X\n", address);
            ++found;
        } else if (result != 2) {
            Serial.printf("  Bus error %u at 0x%02X\n", result, address);
        }
    }
    Serial.printf("  %u responding address(es)\n", found);
}

static void scanBoth() {
    scanBus(Wire1, "Wire1 SDA42/SCL41 (PMU 0x34, PCF8563 RTC 0x51)");
    scanBus(Wire, "Wire SDA17/SCL18 (SH1106 0x3C, BME280 0x77; harmless reserved ACKs 0x7C/0x7E)");
}

static void printHelp() {
    Serial.println("Keys: h help | p rails/power | i scan both buses | o toggle ALDO1+ALDO2 sensors");
    Serial.println("      s arm shutdown, then uppercase Y within 10 s; any other key cancels");
    Serial.println("      d SD check: BLDO1 3.3 V, mount, write/read/delete, unmount, BLDO1 OFF");
    Serial.println("ALDO3 is LoRa: held OFF. DCDC1/core rails are left alone. SD requires FAT32.");
}

static void explainSdMountFailure() {
    // SD.begin() releases its drive on failure, losing the card type. A
    // read-only driver probe distinguishes a nonresponding card/bus from
    // a responding card whose filesystem cannot mount. Never format here.
    uint8_t drive = sdcard_init(SD_CS, &sdSpi, SD_SPI_HZ);
    if (drive == 0xFF) {
        Serial.println("ERROR: SD driver allocation failed; cannot probe card.");
        return;
    }
    if (disk_initialize(drive) & STA_NOINIT) {
        Serial.println("ERROR: no SD card responding -- insert a card, check BLDO1 3.3 V and SPI wiring.");
    } else {
        Serial.println("ERROR: card responds but mount failed -- wrong format, damaged filesystem or read error.");
        Serial.println("Use FAT32 (microSDHC <=32 GB); exFAT on typical 64 GB+ cards is unsupported. Card preserved.");
    }
    sdcard_uninit(drive);
}

static void checkSd() {
    Serial.println("SD check: LilyGO pins MOSI35/SCK36/MISO37/CS47, IMU CS34 HIGH, SPI 4 MHz.");
    // The PMU powers up BLDO1 ON at 1.8 V. Disable first, set 3.3 V,
    // then enable; never briefly enable the card at the default voltage.
    bool powered = PMU.disableBLDO1() && PMU.setBLDO1Voltage(3300) && PMU.enableBLDO1();
    if (powered) {
        pinMode(IMU_CS, OUTPUT);
        digitalWrite(IMU_CS, HIGH);
        pinMode(SD_CS, OUTPUT);
        digitalWrite(SD_CS, HIGH);
        delay(250);  // supply/card settle time before SPI initialization
        if (!sdSpi.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS)) {
            Serial.println("ERROR: SD SPI bus initialization failed; mount skipped.");
        } else if (!SD.begin(SD_CS, sdSpi, SD_SPI_HZ, "/sd", 5, false)) {
            explainSdMountFailure();
        } else if (SD.cardType() == CARD_NONE) {
            Serial.println("ERROR: no SD card detected after mount.");
        } else {
            uint8_t type = SD.cardType();
            Serial.printf("SD card type: %s; size: %llu bytes; total: %llu; used: %llu bytes\n",
                          type == CARD_MMC ? "MMC" : type == CARD_SD ? "SDSC" : "SDHC/SDXC",
                          (unsigned long long)SD.cardSize(), (unsigned long long)SD.totalBytes(),
                          (unsigned long long)SD.usedBytes());
            Serial.println("Filesystem: FAT (exact subtype unavailable through SD API; FAT32 recommended).");
            const char* path = "/stage2-sd-check.txt";
            const char payload[] = "Supreme SD write/read check\n";
            if (SD.exists(path)) {
                Serial.println("ERROR: test path already exists; preserved. Remove it on a PC before retrying.");
            } else {
                File f = SD.open(path, FILE_WRITE);
                bool ok = f && f.write((const uint8_t*)payload, sizeof(payload) - 1) == sizeof(payload) - 1;
                if (f) f.close();  // commit before opening the read-back handle
                File readback = SD.open(path, FILE_READ);
                char bytes[sizeof(payload)] = {0};
                ok = ok && readback && readback.size() == sizeof(payload) - 1 &&
                     readback.read((uint8_t*)bytes, sizeof(payload) - 1) == sizeof(payload) - 1 &&
                     memcmp(bytes, payload, sizeof(payload) - 1) == 0;
                if (readback) readback.close();
                bool removed = SD.remove(path);
                Serial.printf("SD write/read-back: %s; delete: %s\n", ok ? "PASS" : "FAIL", removed ? "PASS" : "FAIL");
            }
        }
    } else {
        Serial.println("ERROR: could not set/enable BLDO1 at 3.3 V; SD mount skipped.");
    }
    SD.end();
    sdSpi.end();
    bool off = PMU.disableBLDO1();
    Serial.printf("SD check finished: unmounted; BLDO1 OFF: %s\n", off ? "OK" : "ERROR");
}

static void toggleSensors() {
    // If either rail is off, turn both on; the next press turns both off.
    bool turnOn = !(PMU.isEnableALDO1() && PMU.isEnableALDO2());
    bool ok;
    if (turnOn) {
        ok = PMU.setALDO1Voltage(3300);
        ok &= PMU.setALDO2Voltage(3300);
        ok &= PMU.enableALDO1();
        ok &= PMU.enableALDO2();
    } else {
        ok = PMU.disableALDO1();
        ok &= PMU.disableALDO2();
    }
    Serial.printf("Sensor rails requested %s at 3.3 V: %s\n", turnOn ? "ON" : "OFF", ok ? "OK" : "ERROR");
    delay(250);  // Let the sensor/OLED supplies settle before the scan.
    printStatus();
    scanBoth();
}

void setup() {
    Serial.begin(115200);
    uint32_t started = millis();
    while (!Serial && millis() - started < 3000) delay(10);
    Serial.println("\nStage 2: Supreme PMU bring-up (no radio initialization)");
    Wire.begin(SENSOR_SDA, SENSOR_SCL);
    Wire1.begin(PMU_SDA, PMU_SCL);
    Wire.setTimeOut(50);
    Wire1.setTimeOut(50);
    pinMode(PMU_IRQ, INPUT);  // Observe only; GPIO40 cannot be an ext1 deep-sleep wake pin.
    if (!PMU.begin(Wire1, AXP2101_SLAVE_ADDRESS, PMU_SDA, PMU_SCL)) {
        Serial.println("ERROR: AXP2101 not found at Wire1 42/41, 0x34. No rail changes made.");
        scanBoth();
        while (true) delay(1000);
    }
    Serial.println("BOOT SNAPSHOT before any sketch rail/ADC changes. ADC readings may be disabled/stale.");
    // begin() identifies the PMU and disables its TS measurement, but does
    // not change rail enable/voltage registers. Record ALL rails first.
    printStatus();
    // VBUS-insert boot left ALDO3 ON; POWER-button boot left it OFF.
    // Disable LoRa first so its boot state cannot leave the radio powered.
    bool ok = PMU.disableALDO3();
    if (!ok || PMU.isEnableALDO3()) {
        Serial.println("ERROR: cannot verify ALDO3 OFF. Attach LoRa antenna; stopping.");
        while (true) delay(1000);
    }
    if (!PMU.disableBLDO1()) {
        Serial.println("ERROR: could not disable BLDO1; SD check will retry before changing voltage.");
    }
    ok = PMU.setALDO4Voltage(3300);
    ok &= PMU.enableALDO4();
    Serial.printf("ALDO3 OFF; ALDO4 GPS requested ON at 3300 mV: %s\n", ok ? "OK" : "ERROR");
    // ADC enable is separate from rail control. Do this AFTER boot capture.
    ok = PMU.enableVbusVoltageMeasure();
    ok &= PMU.enableBattVoltageMeasure();
    ok &= PMU.enableSystemVoltageMeasure();
    if (!ok) Serial.println("WARNING: could not enable all voltage measurements.");
    delay(250);
    printStatus();
    scanBoth();
    printHelp();
}

void loop() {
    if (shutdownPending && millis() - confirmStarted >= 10000) {
        shutdownPending = false;
        Serial.println("Shutdown confirmation expired.");
    }
    while (Serial.available()) {
        char key = Serial.read();
        if (key == '\r' || key == '\n') continue;  // Works with Serial Monitor line endings.
        if (shutdownPending) {
            shutdownPending = false;
            if (key == 'Y' && millis() - confirmStarted < 10000) {
                Serial.println("Shutting down: bench confirmed board off, USB gone; stayed off 15 s with USB connected.");
                Serial.println("USB unplug/replug powered it on in the bench test (#106).");
                Serial.println("Bench sources: power-on 0x04 = VBUS insert; power-off 0x02 = software.");
                Serial.flush();
                delay(100);
                PMU.shutdown();
                Serial.println("WARNING: still executing after shutdown request.");
            } else Serial.println("Shutdown cancelled.");
            continue;
        }
        switch (key) {
            case 'h': printHelp(); break;
            case 'p': printStatus(); break;
            case 'i': scanBoth(); break;
            case 'o': toggleSensors(); break;
            case 'd': checkSd(); break;
            case 's':
                Serial.println("Shutdown test: bench confirmed off for 15 s with USB connected; USB replug powered it on.");
                Serial.println("Press uppercase Y within 10 s to confirm; any other key cancels.");
                shutdownPending = true;
                confirmStarted = millis();
                break;
            default: Serial.println("Unknown key; h shows help."); break;
        }
    }
    static uint32_t lastPrint = 0;
    if (!shutdownPending && millis() - lastPrint >= 5000) {
        lastPrint = millis();
        printStatus();
    }
    delay(1);
}
