// =================================================================
// Stage 3: GPS Fix Test
// =================================================================
// Purpose: Enable GPS power (via the AXP2101 PMU from Stage 2),
// read NMEA data from the NEO-M8N module, and display parsed
// position data when a satellite fix is acquired.
//
// Required libraries (install all via Library Manager):
//   1. XPowersLib by lewisxhe  (same as Stage 2)
//   2. TinyGPSPlus by Mikal Hart
//      Search for "TinyGPSPlus" in Library Manager and install it
//
// CRITICAL: GPS needs open sky to get a fix!
//   - This will NOT work indoors at your desk
//   - Take the board outside or place it on a windowsill
//   - Cold start (first ever fix) takes 30-60 seconds outdoors
//   - Subsequent starts are faster because the module caches data
//
// What to expect:
//   - "Chars received from GPS:" should increment (proves UART works)
//   - "Satellites visible:" should climb from 0 as module acquires
//   - Eventually you'll see lat/lon coordinates when fix is achieved
//
// Troubleshooting:
//   - If "Chars received" stays at 0: PMU didn't enable GPS power,
//     or UART pins are wrong for your board revision
//   - If chars increment but no fix after 5+ min outdoors: antenna
//     connection may be loose, check the IPEX GPS antenna cable
// =================================================================

#include <Wire.h>
#include <XPowersLib.h>
#include <TinyGPSPlus.h>

// -----------------------------------------------------------------
// Pin definitions for T-Beam V1.2
// -----------------------------------------------------------------

// I2C pins for the AXP2101 power manager
#define I2C_SDA 21
#define I2C_SCL 22

// UART pins for the NEO-M8N GPS module
// GPIO 34 receives data FROM the GPS (GPS TX → ESP32 RX)
// GPIO 12 sends data TO the GPS (ESP32 TX → GPS RX)
#define GPS_RX_PIN 34
#define GPS_TX_PIN 12
#define GPS_BAUD   9600

// -----------------------------------------------------------------
// Global objects
// -----------------------------------------------------------------

XPowersAXP2101 PMU;
TinyGPSPlus    gps;
HardwareSerial GPSSerial(1);  // Use hardware UART1 for GPS

// Track whether we've ever gotten a fix, so we can print a
// celebratory message the first time it happens
bool firstFixAchieved = false;

void setup() {
    Serial.begin(115200);
    delay(2000);

    Serial.println("========================================");
    Serial.println("  Stage 3: GPS Fix Test");
    Serial.println("========================================");
    Serial.println();

    // --- Initialize the PMU (same as Stage 2) ---
    Wire.begin(I2C_SDA, I2C_SCL);
    if (!PMU.begin(Wire, AXP2101_SLAVE_ADDRESS, I2C_SDA, I2C_SCL)) {
        Serial.println("ERROR: Failed to find AXP2101!");
        Serial.println("Did Stage 2 work? Check connections.");
        while (1) delay(1000);
    }
    Serial.println("AXP2101 initialized.");

    // Enable GPS power rail (ALDO3 on the T-Beam V1.2 -- see Stage 2)
    PMU.setALDO3Voltage(3300);
    PMU.enableALDO3();
    Serial.println("GPS power enabled (ALDO3 → 3.3V)");

    // Give the GPS module a moment to boot up after power is applied.
    // The NEO-M8N needs a brief moment to initialize its internal
    // circuits before it starts sending NMEA data.
    delay(500);

    // --- Start the GPS UART ---
    // This opens a hardware serial port on the specified pins.
    // Hardware UART is rock solid at 9600 baud — no bit-banging issues.
    GPSSerial.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
    Serial.println("GPS serial opened at 9600 baud");
    Serial.println();
    Serial.println("Waiting for GPS data...");
    Serial.println("(Take the board outside for a satellite fix!)");
    Serial.println();
}

void loop() {
    // -----------------------------------------------------------------
    // Core GPS read loop
    // -----------------------------------------------------------------
    // Every byte that arrives from the GPS module gets fed into
    // TinyGPSPlus, which accumulates characters until it has a
    // complete NMEA sentence, then parses it into structured data.
    while (GPSSerial.available() > 0) {
        char c = GPSSerial.read();
        gps.encode(c);

        // Uncomment the line below to see raw NMEA sentences for debugging.
        // This is very verbose but useful if you suspect the GPS module is
        // sending corrupted data or an unexpected format.
        // Serial.write(c);
    }

    // -----------------------------------------------------------------
    // Status update every 2 seconds
    // -----------------------------------------------------------------
    static unsigned long lastPrint = 0;
    if (millis() - lastPrint > 2000) {
        lastPrint = millis();

        // charsProcessed tells us if the GPS is even talking to us.
        // If this stays at 0, the UART wiring or GPS power is wrong.
        Serial.print("[");
        Serial.print(millis() / 1000);
        Serial.print("s] ");

        Serial.print("Chars from GPS: ");
        Serial.print(gps.charsProcessed());

        Serial.print(" | Sentences parsed: ");
        Serial.print(gps.sentencesWithFix());

        Serial.print(" | Satellites: ");
        Serial.print(gps.satellites.value());

        if (gps.location.isValid()) {
            // We have a fix! Print the coordinates.
            if (!firstFixAchieved) {
                firstFixAchieved = true;
                Serial.println();
                Serial.println("  *** FIRST GPS FIX ACQUIRED! ***");
            }

            Serial.println();
            Serial.print("  Lat:      ");
            Serial.println(gps.location.lat(), 6);
            Serial.print("  Lon:      ");
            Serial.println(gps.location.lng(), 6);
            Serial.print("  Alt:      ");
            Serial.print(gps.altitude.meters(), 1);
            Serial.println(" m");
            Serial.print("  Speed:    ");
            Serial.print(gps.speed.mph(), 1);
            Serial.println(" mph");
            Serial.print("  Course:   ");
            Serial.print(gps.course.deg(), 1);
            Serial.println(" deg");
            Serial.print("  HDOP:     ");
            Serial.println(gps.hdop.hdop(), 2);

            // Print the GPS time (UTC)
            if (gps.date.isValid() && gps.time.isValid()) {
                Serial.print("  GPS Time: ");
                Serial.print(gps.date.year());
                Serial.print("-");
                if (gps.date.month() < 10) Serial.print("0");
                Serial.print(gps.date.month());
                Serial.print("-");
                if (gps.date.day() < 10) Serial.print("0");
                Serial.print(gps.date.day());
                Serial.print("T");
                if (gps.time.hour() < 10) Serial.print("0");
                Serial.print(gps.time.hour());
                Serial.print(":");
                if (gps.time.minute() < 10) Serial.print("0");
                Serial.print(gps.time.minute());
                Serial.print(":");
                if (gps.time.second() < 10) Serial.print("0");
                Serial.print(gps.time.second());
                Serial.println("Z");
            }
        } else {
            // Still waiting for a fix
            Serial.println(" | No fix yet");
        }

        // Warn if we're not receiving any data at all
        if (gps.charsProcessed() < 10) {
            Serial.println("  WARNING: No data from GPS module!");
            Serial.println("  Check: Is GPS power enabled? (Stage 2)");
            Serial.println("  Check: Are UART pins correct for your board?");
            Serial.println("  Check: Is the GPS antenna connected?");
        }
    }
}
