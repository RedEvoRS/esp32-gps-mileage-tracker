// =================================================================
// Stage 2: Supreme AXP2101 / Both I2C Buses / Shutdown Test
// =================================================================
// Purpose: Record boot rails before changing them, safely power the GPS,
// and make the shutdown/VBUS power-on experiment in #106 repeatable.
// What to expect: a boot snapshot, ALDO3 OFF, ALDO4 at 3.3 V, bus scans,
// then status every 5 s. Keys: h help, p status, i scan, o sensors,
// s then uppercase Y within 10 s to shut down (any other key cancels).
// PMU = Wire1 SDA 42/SCL 41 at 0x34; sensors = Wire SDA 17/SCL 18.
// OLED may answer at 0x3C (or 0x3D), PCF8563 RTC at 0x51.
// Requires installed XPowersLib 0.3.3; do not substitute a different API.
//
// Supreme upload settings (for the owner when the board arrives):
//   Before ANY power, attach the 915 MHz LoRa antenna: factory firmware
//   may transmit. Back up the factory flash before the first upload.
//   Arduino IDE: ESP32S3 Dev Module, USB CDC On Boot Enabled,
//   USB Mode Hardware CDC and JTAG, Flash Size 8 MB, PSRAM QSPI PSRAM.
//   CLI FQBN (ESP32 core 3.3.11):
//   esp32:esp32:esp32s3:CDCOnBoot=cdc,USBMode=hwcdc,FlashSize=8M,PSRAM=enabled
//   Select the Supreme's port, then Upload. If native USB disappears,
//   hold BOOT, tap RST, release BOOT; select the new port and retry.
//   QSPI follows LilyGO's boards/t-beams3-supreme.json (qio_qspi), but is
//   unconfirmed: use Stage 1's PSRAM size; if zero try OPI (PSRAM=opi).
//
// Pin/rail source: Xinyuan-LilyGO/LilyGo-LoRa-Series, commit
// 1991b1ed70915d4f246b841f99f6027166ea7330, examples/Factory/utilities.h
// and LoRaBoards.cpp, T_BEAM_S3_SUPREME sections. Bench checks pending.
// Unlike LilyGO's factory setup, never enable ALDO3 (LoRa) here, and
// never configure/disable DCDC1 (ESP32), other core rails, or BLDO1 (SD).
// =================================================================

#include <Wire.h>
#include <XPowersLib.h>

static const int PMU_SDA = 42, PMU_SCL = 41, PMU_IRQ = 40;
static const int SENSOR_SDA = 17, SENSOR_SCL = 18;
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
    Serial.println("  VBACKUP = button-battery charge enable/target; actual GPS backup wiring unknown.");
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
    scanBus(Wire1, "Wire1 SDA42/SCL41 (PMU expected 0x34)");
    scanBus(Wire, "Wire SDA17/SCL18 (OLED ~0x3C, RTC 0x51)");
}

static void printHelp() {
    Serial.println("Keys: h help | p rails/power | i scan both buses | o toggle ALDO1+ALDO2 sensors");
    Serial.println("      s arm shutdown, then uppercase Y within 10 s; any other key cancels");
    Serial.println("ALDO3 is LoRa: held OFF. DCDC1/core rails and BLDO1/SD are left alone.");
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
    bool ok = PMU.disableALDO3();
    if (!ok || PMU.isEnableALDO3()) {
        Serial.println("ERROR: cannot verify ALDO3 OFF. Attach LoRa antenna; stopping.");
        while (true) delay(1000);
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
                Serial.println("Shutting down: board goes dark and native USB disappears.");
                Serial.println("Unplug/replug USB: it should boot IF VBUS insert powers on this PMU.");
                Serial.println("If it stays dark, press PWR. Record next boot's power-on source for #106.");
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
            case 's':
                Serial.println("Shutdown test: board goes dark. Unplug/replug USB to test VBUS power-on; use PWR if needed.");
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
