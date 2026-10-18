// =================================================================
// Stage 3: Supreme MAX-M10S GPS / Rail / Legacy UBX Test
// =================================================================
// Purpose: Make #105's GPS wiring/backup checks and #108's UBX checks
// testable without changing Stage 4. No receiver configuration at boot.
// Requires XPowersLib 0.3.3 and TinyGPSPlus 1.0.3 (ESP32 core 3.3.11).
// What to expect: boot rail snapshot, ALDO3 OFF, ALDO4 power cycle at
// 3.3 V, GPS_EN HIGH, UART RX9/TX8 at 9600; probe other bauds if silent.
// Summary every 2 s: fresh fix, satellites USED, HDOP, NMEA counts,
// checksum failures, PPS rate on GPIO6, and time to first fix (TTFF).
// Use open sky and a connected GPS antenna. No fix indoors is normal.
// Coordinates are OFF by default: serial logs may be pasted into issues.
// l opts into coordinates; r opts into raw NMEA (also contains location).
// h lists keys. 4 toggles GPS rail; e toggles GPS_EN; b toggles VBACKUP
// for completeness: VBACKUP is not needed for hot starts on this board.
// 3 then uppercase Y within 10 s enables ALDO3 ONLY WITH LoRa antenna.
// u sends Stage 4's CFG-MSG GLL/GSA/GSV/VTG + CFG-NAV5: SPG 5.10
// ACKed all five. VALSET is future-proofing tracked in #108.
// v polls MON-VER: bench ROM SPG 5.10, PROTVER=34.10, MOD=MAX-M10S.
// g polls NAV-SAT (signal strength per satellite) and MON-RF (antenna/AGC).
// Bench: unplugged/dead antenna gave heard=0, AGC stuck around 79%; a
// connected active antenna outdoors gave ~19-25 satellites, 42-48 dBHz,
// AGC ~29%. This board powers an active GPS antenna.
//
// Bench 2026-10-18: NMEA at 9600 on RX9/TX8. ALDO4 is the GPS rail:
// OFF = silent, ON = talking, with ALDO3 OFF throughout. GPS_EN (GPIO7)
// LOW vs HIGH made no difference on MAX-M10S; keep HIGH like LilyGO.
// GPS backup RAM uses the 18650, not PMU VBACKUP. With VBACKUP OFF,
// a hot start after 60 s ALDO4 OFF took 2.0 s; pulling the battery wiped
// backup RAM, and the next cold start took 32.8 s. Saved factory settings
// also survived PMU shutdowns until the battery was pulled.
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
// Pin/rail/baud source: Xinyuan-LilyGO/LilyGo-LoRa-Series, commit
// 1991b1ed70915d4f246b841f99f6027166ea7330, examples/Factory/utilities.h
// and LoRaBoards.cpp (Supreme). DCDC1/core rails and BLDO1/SD untouched.
// No LoRa initialization. VBACKUP is left as found until the b key.
// =================================================================

#include <Wire.h>
#include <XPowersLib.h>
#include <TinyGPSPlus.h>

static const int PMU_SDA = 42, PMU_SCL = 41, PMU_IRQ = 40;
static const int GPS_RX = 9, GPS_TX = 8, GPS_EN = 7, GPS_PPS = 6;
static const uint32_t BAUDS[] = {9600, 38400, 115200, 19200, 57600};
XPowersAXP2101 PMU;
TinyGPSPlus gps;
HardwareSerial GPSSerial(1);
bool rawEcho = false, showCoordinates = false, gpsEnHigh = true;
bool radioConfirm = false, baudLocked = false, firstFix = false;
bool gpsRailOn = false;
uint32_t confirmStarted = 0, gpsPoweredAt = 0, firstFixMs = 0;
uint32_t probeStarted = 0, lastNmeaAt = 0;
size_t baudIndex = 0;
int knownBaudIndex = -1;  // Last baud that produced valid NMEA, -1 = none yet.
volatile uint32_t ppsPulses = 0;
portMUX_TYPE ppsMux = portMUX_INITIALIZER_UNLOCKED;

// A bounded UBX decoder checks BOTH checksum bytes before accepting ACK
// or MON-VER. Binary responses are not passed to the NMEA decoder/echo.
uint8_t ubxState = 0, ubxClass = 0, ubxId = 0, ubxA = 0, ubxB = 0;
uint8_t ubxReceivedA = 0;
uint16_t ubxLength = 0, ubxOffset = 0;
uint8_t ubxPayload[1024];
uint32_t ubxLastByte = 0;
uint8_t ackClass = 0, ackId = 0;
bool awaitingAck = false, awaitingVersion = false, versionReceived = false;
bool awaitingSat = false, satReceived = false, awaitingRf = false, rfReceived = false;
int ackResult = -1;

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

static void ARDUINO_ISR_ATTR onPps() {
    portENTER_CRITICAL_ISR(&ppsMux);
    ppsPulses = ppsPulses + 1;
    portEXIT_CRITICAL_ISR(&ppsMux);
}

static void printVersionField(const char* label, const uint8_t* bytes, size_t length) {
    Serial.print(label);
    for (size_t i = 0; i < length && bytes[i] != 0; ++i) {
        Serial.write(bytes[i] >= 32 && bytes[i] <= 126 ? bytes[i] : '.');
    }
    Serial.println();
}

static uint16_t le16(const uint8_t* p) { return (uint16_t)(p[0] | (p[1] << 8)); }

static void printNavSat() {
    // UBX-NAV-SAT: 8-byte header, then 12 bytes per satellite. Signal
    // strength (C/N0) tells a dead/unpowered antenna (nothing heard) from a
    // poor sky view (weak signals). Sky positions are not printed.
    uint8_t numSvs = ubxPayload[5];
    if (ubxLength < 8 || ubxLength != 8 + 12u * numSvs) {
        Serial.println("NAV-SAT: malformed length; ignoring.");
        return;
    }
    static const char kGnss[] = "GSEBIQR";  // GPS, SBAS, Galileo, BeiDou, IMES, QZSS, GLONASS
    unsigned heard = 0, used = 0, maxCno = 0, sumCno = 0;
    Serial.printf("NAV-SAT: %u satellites known. Heard (C/N0 dBHz, * = used in fix):\n ", numSvs);
    for (unsigned i = 0; i < numSvs; ++i) {
        const uint8_t* sv = ubxPayload + 8 + 12 * i;
        uint8_t cno = sv[2];
        bool svUsed = sv[8] & 0x08;
        if (svUsed) ++used;
        if (!cno) continue;
        ++heard;
        sumCno += cno;
        if (cno > maxCno) maxCno = cno;
        Serial.printf(" %c%02u:%u%s", sv[0] < 7 ? kGnss[sv[0]] : '?', sv[1], cno, svUsed ? "*" : "");
        if (heard % 10 == 0) Serial.print("\n ");
    }
    Serial.printf("\n  heard=%u used=%u max=%u avg=%u dBHz\n", heard, used, maxCno, heard ? sumCno / heard : 0);
    Serial.println("  C/N0 guide: none = no signal reaching the receiver; <25 weak; 30-45 good sky view.");
}

static void printMonRf() {
    // UBX-MON-RF: 4-byte header, then 24 bytes per RF block.
    uint8_t nBlocks = ubxPayload[1];
    if (ubxLength < 4 || ubxLength != 4 + 24u * nBlocks) {
        Serial.println("MON-RF: malformed length; ignoring.");
        return;
    }
    static const char* kAnt[] = {"INIT", "DONTKNOW", "OK", "SHORT", "OPEN"};
    static const char* kPower[] = {"OFF", "ON", "DONTKNOW"};
    static const char* kJam[] = {"unknown", "ok", "warning", "critical"};
    for (unsigned i = 0; i < nBlocks; ++i) {
        const uint8_t* b = ubxPayload + 4 + 24 * i;
        uint16_t agc = le16(b + 14);
        Serial.printf("MON-RF block %u: antenna %s, antenna power %s, jamming %s, noise %u/ms, AGC %u (%u%% of max)\n",
                      b[0], b[2] < 5 ? kAnt[b[2]] : "?", b[3] < 3 ? kPower[b[3]] : "?",
                      kJam[b[1] & 0x03], le16(b + 12), agc, (unsigned)(agc * 100UL / 8191));
    }
    Serial.println("  Antenna status/power need the antenna supervisor set up; DONTKNOW is normal.");
    Serial.println("  AGC near max = receiver at full gain = little signal coming in.");
}

static void handleUbx() {
    if (awaitingSat && ubxClass == 0x01 && ubxId == 0x35) { printNavSat(); satReceived = true; }
    if (awaitingRf && ubxClass == 0x0A && ubxId == 0x38) { printMonRf(); rfReceived = true; }
    if (awaitingAck && ubxClass == 0x05 && ubxLength == 2 &&
        ubxPayload[0] == ackClass && ubxPayload[1] == ackId) {
        if (ubxId == 0x01) ackResult = 1;
        if (ubxId == 0x00) ackResult = 0;
    }
    if (awaitingVersion && ubxClass == 0x0A && ubxId == 0x04) {
        if (ubxLength < 40 || (ubxLength - 40) % 30 != 0) {
            Serial.println("MON-VER: malformed length; ignoring.");
            return;
        }
        printVersionField("MON-VER software: ", ubxPayload, 30);
        printVersionField("MON-VER hardware: ", ubxPayload + 30, 10);
        for (size_t offset = 40; offset < ubxLength; offset += 30) {
            printVersionField("MON-VER extension: ", ubxPayload + offset, 30);
        }
        versionReceived = true;
    }
}

static void feedNmea(uint8_t byte) {
    if (rawEcho) Serial.write(byte);
    if (!gps.encode((char)byte)) return;
    lastNmeaAt = millis();  // Only a complete, checksum-valid sentence gets here.
    if (!baudLocked) {
        baudLocked = true;
        knownBaudIndex = (int)baudIndex;
        Serial.printf("\nValid NMEA detected at %lu baud.\n", (unsigned long)BAUDS[baudIndex]);
    }
    if (gpsRailOn && !firstFix && gps.location.isUpdated() &&
        gps.location.isValid() && gps.location.age() < 1000) {
        firstFix = true;
        firstFixMs = millis() - gpsPoweredAt;
        Serial.printf("\nFIRST FIX: TTFF %lu ms since ALDO4 switched ON.\n", (unsigned long)firstFixMs);
    }
}

static void receiveByte(uint8_t byte) {
    // An incomplete/corrupted frame must not swallow NMEA indefinitely.
    if (ubxState && millis() - ubxLastByte > 500) ubxState = 0;
    ubxLastByte = millis();
    switch (ubxState) {
        case 0:
            if (byte == 0xB5) ubxState = 1;
            else feedNmea(byte);
            return;
        case 1:
            if (byte == 0x62) { ubxState = 2; ubxA = ubxB = 0; }
            else { ubxState = byte == 0xB5 ? 1 : 0; if (!ubxState) feedNmea(byte); }
            return;
        case 2: ubxClass = byte; ubxState = 3; break;
        case 3: ubxId = byte; ubxState = 4; break;
        case 4: ubxLength = byte; ubxState = 5; break;
        case 5:
            ubxLength |= (uint16_t)byte << 8;
            ubxOffset = 0;
            ubxState = ubxLength ? 6 : 7;
            break;
        case 6:
            if (ubxOffset < sizeof(ubxPayload)) ubxPayload[ubxOffset] = byte;
            if (++ubxOffset == ubxLength) ubxState = 7;
            break;
        case 7: ubxReceivedA = byte; ubxState = 8; return;
        case 8:
            if (ubxReceivedA == ubxA && byte == ubxB) {
                if (ubxLength <= sizeof(ubxPayload)) handleUbx();
                else Serial.println("UBX frame too large for diagnostic buffer; ignored.");
            } else Serial.println("UBX checksum failed; frame ignored.");
            ubxState = 0;
            return;
    }
    ubxA += byte;
    ubxB += ubxA;
}

static void pumpGps() {
    while (GPSSerial.available()) receiveByte((uint8_t)GPSSerial.read());
}

static void resetDecoder() {
    // Do not allow a buffered/pre-power-cycle fix to satisfy the new TTFF.
    while (GPSSerial.available()) GPSSerial.read();
    gps = TinyGPSPlus();
    ubxState = 0;
    lastNmeaAt = 0;
    baudLocked = false;
    probeStarted = millis();
}

static void selectBaud(size_t index) {
    baudIndex = index;
    GPSSerial.end();
    GPSSerial.begin(BAUDS[index], SERIAL_8N1, GPS_RX, GPS_TX);
    resetDecoder();
    Serial.printf("Probing GPS RX9/TX8 at %lu baud (4 s window)...\n", (unsigned long)BAUDS[index]);
}

static void resumeKnownBaud() {
    // Go straight back to the baud that last worked, so a hot start isn't
    // timed while the probe cycles through wrong bauds (inflates TTFF).
    if (knownBaudIndex >= 0 && (size_t)knownBaudIndex != baudIndex) selectBaud(knownBaudIndex);
}

static void checkBaud() {
    // A silent receiver with ALDO4 off is expected: don't wander off the
    // known-good baud while the rail is off.
    if (baudLocked || !gpsRailOn || millis() - probeStarted < 4000) return;
    Serial.printf("No valid NMEA at %lu baud.\n", (unsigned long)BAUDS[baudIndex]);
    selectBaud((baudIndex + 1) % (sizeof(BAUDS) / sizeof(BAUDS[0])));
}

static void ubxSend(uint8_t cls, uint8_t id, const uint8_t* payload, uint16_t length) {
    // Same frame and payload bytes as Stage 4; zero length is a poll.
    uint8_t header[6] = {0xB5, 0x62, cls, id, (uint8_t)length, (uint8_t)(length >> 8)};
    uint8_t a = 0, b = 0;
    for (int i = 2; i < 6; ++i) { a += header[i]; b += a; }
    for (uint16_t i = 0; i < length; ++i) { a += payload[i]; b += a; }
    GPSSerial.write(header, sizeof(header));
    if (length) GPSSerial.write(payload, length);
    GPSSerial.write(a);
    GPSSerial.write(b);
}

static int ubxCommand(uint8_t cls, uint8_t id, const uint8_t* payload, uint16_t length) {
    // Stage 4's policy: three attempts, 300 ms each, stop on ACK or NAK.
    pumpGps();
    ackClass = cls;
    ackId = id;
    awaitingAck = true;
    ackResult = -1;
    for (int attempt = 0; attempt < 3 && ackResult < 0; ++attempt) {
        ubxSend(cls, id, payload, length);
        uint32_t sentAt = millis();
        while (ackResult < 0 && millis() - sentAt < 300) { pumpGps(); delay(1); }
    }
    awaitingAck = false;
    return ackResult;
}

static const char* ackText(int result) {
    return result == 1 ? "ACK" : result == 0 ? "NAK" : "no answer";
}

static void testLegacyConfig() {
    Serial.println("Sending Stage 4 legacy config: SPG 5.10 ACKed all five on the bench; VALSET is future-proofing (#108).");
    Serial.println("If accepted, these RAM settings silence GLL/GSA/GSV/VTG and select automotive.");
    static const uint8_t silence[] = {0x01, 0x02, 0x03, 0x05};
    static const char* names[] = {"GLL", "GSA", "GSV", "VTG"};
    for (size_t i = 0; i < sizeof(silence); ++i) {
        uint8_t payload[3] = {0xF0, silence[i], 0};
        int result = ubxCommand(0x06, 0x01, payload, sizeof(payload));
        Serial.printf("CFG-MSG %s: %s\n", names[i], ackText(result));
    }
    // Exactly configureGps()'s 36 bytes: mask bit 0, dynModel=4, rest zero.
    uint8_t nav5[36] = {0};
    nav5[0] = 0x01;
    nav5[2] = 4;
    int result = ubxCommand(0x06, 0x24, nav5, sizeof(nav5));
    Serial.printf("CFG-NAV5 automotive: %s\n", ackText(result));
    // Unlike Stage 4's early exit, diagnostics try every message even if
    // the first is silent, so every requested result appears in the log.
}

static void pollVersion() {
    pumpGps();
    awaitingVersion = true;
    versionReceived = false;
    ubxSend(0x0A, 0x04, nullptr, 0);  // UBX-MON-VER poll.
    uint32_t sentAt = millis();
    while (!versionReceived && millis() - sentAt < 2000) { pumpGps(); delay(1); }
    awaitingVersion = false;
    if (!versionReceived) Serial.println("MON-VER: no valid answer within 2 s. Check baud, TX8, GPS power/EN.");
}

static void pollSignal() {
    // Poll satellite signal strengths, then the RF front end.
    pumpGps();
    awaitingSat = true;
    satReceived = false;
    ubxSend(0x01, 0x35, nullptr, 0);  // UBX-NAV-SAT poll.
    uint32_t sentAt = millis();
    while (!satReceived && millis() - sentAt < 2000) { pumpGps(); delay(1); }
    awaitingSat = false;
    if (!satReceived) Serial.println("NAV-SAT: no valid answer within 2 s.");
    awaitingRf = true;
    rfReceived = false;
    ubxSend(0x0A, 0x38, nullptr, 0);  // UBX-MON-RF poll.
    sentAt = millis();
    while (!rfReceived && millis() - sentAt < 2000) { pumpGps(); delay(1); }
    awaitingRf = false;
    if (!rfReceived) Serial.println("MON-RF: no valid answer within 2 s.");
}

static void toggleGpsPower() {
    bool turnOn = !PMU.isEnableALDO4();
    resetDecoder();
    bool ok;
    if (turnOn) {
        ok = PMU.setALDO4Voltage(3300);
        ok &= PMU.enableALDO4();
        gpsPoweredAt = millis();
        firstFix = false;
        firstFixMs = 0;
    } else ok = PMU.disableALDO4();
    gpsRailOn = PMU.isEnableALDO4();
    Serial.printf("ALDO4 requested %s: %s, readback=%s, %u mV\n",
                  turnOn ? "ON" : "OFF", ok ? "OK" : "ERROR",
                  gpsRailOn ? "ON" : "OFF", PMU.getALDO4Voltage());
    if (turnOn && gpsRailOn) Serial.println("TTFF restarted from ALDO4 ON. Bench hot start: 2.0 s after 60 s OFF, VBACKUP OFF.");
    if (!turnOn) Serial.println("ALDO4 OFF: bench NMEA stopped; ON restored it, with ALDO3 OFF throughout.");
    // Keep receiving during rail-off checks; bench NMEA stopped with ALDO4 OFF.
    if (turnOn) resumeKnownBaud();
    // Restart the probe window to give the current baud a full 4 seconds.
    probeStarted = millis();
}

static void toggleBackup() {
    bool turnOn = !PMU.isEnableButtonBatteryCharge();
    bool ok;
    if (turnOn) {
        // b is kept for completeness: GPS backup RAM uses the 18650.
        // Same calls/target as Stage 4; LilyGO leaves VBACKUP OFF here.
        ok = PMU.setButtonBatteryChargeVoltage(3300);
        ok &= PMU.enableButtonBatteryCharge();
    } else ok = PMU.disableButtonBatteryCharge();
    Serial.printf("VBACKUP charge requested %s: %s, readback=%s, target=%u mV\n",
                  turnOn ? "ON" : "OFF", ok ? "OK" : "ERROR",
                  PMU.isEnableButtonBatteryCharge() ? "ON" : "OFF", PMU.getButtonBatteryVoltage());
    Serial.println("GPS backup uses the 18650; VBACKUP is not needed for hot starts on this board.");
}

static void printHelp() {
    Serial.println("Keys: h help | r raw NMEA (includes private coordinates!) | l coordinates (default OFF)");
    Serial.println("      4 ALDO4 GPS power/TTFF reset | e GPS_EN HIGH/LOW | b VBACKUP charge (optional)");
    Serial.println("      3 ALDO3 LoRa toggle: ON requires 3 then uppercase Y within 10 s and antenna");
    Serial.println("      u legacy UBX (SPG 5.10 accepts; VALSET #108) | v MON-VER | p rails/power");
    Serial.println("      g satellite signal strengths (NAV-SAT) + RF/antenna status (MON-RF)");
    Serial.println("Counters reset on baud/power/EN changes; TTFF resets only on ALDO4 ON.");
    Serial.println("GPS backup uses the 18650; b is kept for completeness, not needed for hot starts.");
}

static void handleKey(char key) {
    if (key == '\r' || key == '\n') return;
    if (radioConfirm) {
        radioConfirm = false;
        if (key == 'Y' && millis() - confirmStarted < 10000) {
            bool ok = PMU.setALDO3Voltage(3300);
            ok &= PMU.enableALDO3();
            Serial.printf("ALDO3 LoRa requested ON at 3300 mV: %s, readback=%s\n",
                          ok ? "OK" : "ERROR", PMU.isEnableALDO3() ? "ON" : "OFF");
            resetDecoder();
        } else Serial.println("ALDO3 enable cancelled.");
        return;
    }
    switch (key) {
        case 'h': printHelp(); break;
        case 'p': printStatus(); break;
        case 'r':
            rawEcho = !rawEcho;
            Serial.println(rawEcho ? "WARNING: raw NMEA ON includes coordinates. Redact before sharing logs!" : "Raw NMEA OFF.");
            break;
        case 'l':
            showCoordinates = !showCoordinates;
            Serial.println(showCoordinates ? "Coordinates ON: private location, redact before sharing logs!" : "Parsed coordinates OFF. Raw echo is controlled separately by r.");
            break;
        case '4': toggleGpsPower(); break;
        case 'e':
            gpsEnHigh = !gpsEnHigh;
            digitalWrite(GPS_EN, gpsEnHigh ? HIGH : LOW);
            resetDecoder();
            if (gpsEnHigh) resumeKnownBaud();
            Serial.printf("GPS_EN GPIO7=%s. Bench: LOW/HIGH made no difference to MAX-M10S NMEA; default HIGH follows LilyGO.\n", gpsEnHigh ? "HIGH" : "LOW");
            break;
        case 'b': toggleBackup(); break;
        case '3':
            if (PMU.isEnableALDO3()) {
                bool ok = PMU.disableALDO3();
                Serial.printf("ALDO3 LoRa requested OFF: %s, readback=%s\n", ok ? "OK" : "ERROR", PMU.isEnableALDO3() ? "ON" : "OFF");
                resetDecoder();
            } else {
                Serial.println("WARNING: ALDO3 powers SX1262 LoRa. Attach the 915 MHz LoRa antenna BEFORE enabling!");
                Serial.println("Press uppercase Y within 10 s ONLY with antenna attached; any other key cancels.");
                radioConfirm = true;
                confirmStarted = millis();
            }
            break;
        case 'u': testLegacyConfig(); break;
        case 'v': pollVersion(); break;
        case 'g': pollSignal(); break;
        default: Serial.println("Unknown key; h shows help."); break;
    }
}

static void printSummary(uint32_t elapsedMs) {
    static uint32_t previousPulses = 0;
    portENTER_CRITICAL(&ppsMux);
    uint32_t pulses = ppsPulses;
    portEXIT_CRITICAL(&ppsMux);
    float ppsRate = (pulses - previousPulses) * 1000.0f / elapsedMs;
    previousPulses = pulses;
    bool freshFix = gps.location.isValid() && gps.location.age() < 3000;
    Serial.printf("[%lus] baud=%lu NMEA=%s | fix=%s | sats=",
                  (unsigned long)(millis() / 1000), (unsigned long)BAUDS[baudIndex],
                  baudLocked && millis() - lastNmeaAt < 3000 ? "fresh" : "silent/probing",
                  freshFix ? "valid/fresh" : "none/stale");
    if (gps.satellites.isValid() && gps.satellites.age() < 3000) Serial.print(gps.satellites.value());
    else Serial.print("n/a");
    Serial.print(" | HDOP=");
    if (gps.hdop.isValid() && gps.hdop.age() < 3000) Serial.print(gps.hdop.hdop(), 2);
    else Serial.print("n/a");
    Serial.printf(" | chars=%lu sentences=%lu withFix=%lu checksumFail=%lu | PPS=%.2f/s (%lu total) | TTFF=",
                  (unsigned long)gps.charsProcessed(), (unsigned long)gps.passedChecksum(),
                  (unsigned long)gps.sentencesWithFix(), (unsigned long)gps.failedChecksum(),
                  ppsRate, (unsigned long)pulses);
    if (!gpsRailOn) Serial.println("ALDO4 OFF");
    else if (firstFix) Serial.printf("%lu ms\n", (unsigned long)firstFixMs);
    else Serial.printf("waiting (%lu ms since ALDO4 ON)\n", (unsigned long)(millis() - gpsPoweredAt));
    if (showCoordinates && freshFix) {
        Serial.printf("  PRIVATE coordinates: %.6f, %.6f\n", gps.location.lat(), gps.location.lng());
    }
}

void setup() {
    Serial.begin(115200);
    uint32_t started = millis();
    while (!Serial && millis() - started < 3000) delay(10);
    Serial.println("\nStage 3: Supreme MAX-M10S GPS bring-up");
    Serial.println("Coordinates and raw NMEA OFF: logs may be shared publicly; location is private.");
    Wire1.begin(PMU_SDA, PMU_SCL);
    Wire1.setTimeOut(50);
    pinMode(PMU_IRQ, INPUT);
    if (!PMU.begin(Wire1, AXP2101_SLAVE_ADDRESS, PMU_SDA, PMU_SCL)) {
        Serial.println("ERROR: AXP2101 missing at Wire1 42/41, 0x34. No rail changes made.");
        while (true) delay(1000);
    }
    Serial.println("BOOT SNAPSHOT before sketch rail/ADC changes (ADC may be disabled/stale).");
    printStatus();
    // VBUS-insert boot left ALDO3 ON; POWER-button boot left it OFF.
    // Disable LoRa first so its boot state cannot leave the radio powered.
    bool ok = PMU.disableALDO3();
    if (!ok || PMU.isEnableALDO3()) {
        Serial.println("ERROR: cannot verify ALDO3 OFF. Attach LoRa antenna; stopping.");
        while (true) delay(1000);
    }
    // Establish a known OFF -> ON edge; boot may have left GPS powered.
    ok = PMU.disableALDO4();
    if (!ok || PMU.isEnableALDO4()) {
        Serial.println("ERROR: cannot switch ALDO4 OFF for a known TTFF epoch; stopping.");
        while (true) delay(1000);
    }
    delay(250);
    pinMode(GPS_EN, OUTPUT);
    digitalWrite(GPS_EN, HIGH);  // LilyGO HIGH; bench LOW/HIGH made no NMEA difference.
    pinMode(GPS_PPS, INPUT);
    attachInterrupt(digitalPinToInterrupt(GPS_PPS), onPps, RISING);
    selectBaud(0);
    toggleGpsPower();
    if (!gpsRailOn || PMU.getALDO4Voltage() != 3300) {
        Serial.println("ERROR: cannot verify GPS ALDO4 ON at 3300 mV; stopping.");
        while (true) delay(1000);
    }
    ok = PMU.enableVbusVoltageMeasure();
    ok &= PMU.enableBattVoltageMeasure();
    ok &= PMU.enableSystemVoltageMeasure();
    if (!ok) Serial.println("WARNING: could not enable all voltage measurements.");
    // Leave VBACKUP as found; b is optional, not needed for GPS hot starts.
    printStatus();
    printHelp();
}

void loop() {
    pumpGps();
    if (radioConfirm && millis() - confirmStarted >= 10000) {
        radioConfirm = false;
        Serial.println("ALDO3 enable confirmation expired.");
    }
    while (Serial.available()) { handleKey(Serial.read()); pumpGps(); }
    checkBaud();
    static uint32_t lastPrint = 0;
    uint32_t now = millis();
    if (now - lastPrint >= 2000) {
        printSummary(now - lastPrint);
        lastPrint = now;
    }
    delay(1);
}
