// =================================================================
// Stage 2: AXP2101 Power Manager Test
// =================================================================
// Purpose: Initialize the AXP2101 PMU and enable the GPS power rail.
// This is a critical step because on newer T-Beam boards, the GPS
// module is powered OFF by default at boot. If you skip this step,
// the GPS will never produce any data.
//
// Required library:
//   XPowersLib by lewisxhe
//   Install via: Sketch → Include Library → Manage Libraries
//   Search for "XPowersLib" and click Install
//
// What to expect:
//   - Serial Monitor should show "AXP2101 found and initialized!"
//   - Battery voltage will be reported (0mV if no battery inserted)
//   - Battery percentage will update every 5 seconds
//
// If "Failed to find AXP2101" appears:
//   - Check that you're using the correct I2C pins (21/22)
//   - Verify your board is actually a V1.2 (look at the PCB silkscreen)
//   - Try a different USB cable or power cycle the board
// =================================================================

#include <Wire.h>
#include <XPowersLib.h>

// The AXP2101 communicates over I2C. On the T-Beam, the I2C bus
// is wired to GPIO 21 (SDA) and GPIO 22 (SCL). The chip lives at
// I2C address 0x34.
#define I2C_SDA 21
#define I2C_SCL 22

XPowersAXP2101 PMU;

void setup() {
    Serial.begin(115200);
    delay(2000);  // Give the serial port a moment to come up

    Serial.println("========================================");
    Serial.println("  Stage 2: AXP2101 PMU Test");
    Serial.println("========================================");
    Serial.println();

    // Start the I2C bus on the correct pins
    Wire.begin(I2C_SDA, I2C_SCL);

    // Try to talk to the AXP2101. If this fails, either the chip
    // isn't responding or we're on the wrong pins.
    if (!PMU.begin(Wire, AXP2101_SLAVE_ADDRESS, I2C_SDA, I2C_SCL)) {
        Serial.println("ERROR: Failed to find AXP2101!");
        Serial.println("Check wiring, board version, or power source.");
        while (1) delay(1000);  // Halt here for debugging
    }
    Serial.println("AXP2101 found and initialized!");
    Serial.println();

    // On the T-Beam V1.2, the GPS is powered by the ALDO3 rail at 3.3V
    // (measured on this board 2026-10-05: ALDO3 off silences the GPS,
    // ALDO4 off doesn't). ALDO3 happens to be on at power-up, but enable
    // it explicitly rather than rely on that.
    PMU.setALDO3Voltage(3300);
    PMU.enableALDO3();
    Serial.println("GPS power rail (ALDO3) enabled at 3.3V");

    // Report initial battery status so we can confirm the PMU
    // is reading things correctly.
    Serial.println();
    Serial.print("Battery voltage:  ");
    Serial.print(PMU.getBattVoltage());
    Serial.println(" mV");

    Serial.print("USB voltage:      ");
    Serial.print(PMU.getVbusVoltage());
    Serial.println(" mV");

    Serial.print("USB connected:    ");
    Serial.println(PMU.isVbusIn() ? "Yes" : "No");

    Serial.print("Battery present:  ");
    Serial.println(PMU.isBatteryConnect() ? "Yes" : "No");

    if (PMU.isBatteryConnect()) {
        Serial.print("Battery percent:  ");
        Serial.print(PMU.getBatteryPercent());
        Serial.println("%");

        Serial.print("Charging:         ");
        Serial.println(PMU.isCharging() ? "Yes" : "No");
    }

    Serial.println();
    Serial.println("PMU setup complete. Reporting battery status every 5 seconds...");
    Serial.println();
}

void loop() {
    // Periodically report battery status so we know it's alive
    // and can monitor charging if a battery is inserted.
    Serial.print("Battery: ");
    if (PMU.isBatteryConnect()) {
        Serial.print(PMU.getBatteryPercent());
        Serial.print("% (");
        Serial.print(PMU.getBattVoltage());
        Serial.print("mV)");
        if (PMU.isCharging()) {
            Serial.print(" [CHARGING]");
        }
    } else {
        Serial.print("not connected");
    }
    Serial.print(" | USB: ");
    Serial.print(PMU.isVbusIn() ? "connected" : "disconnected");
    Serial.println();

    delay(5000);
}
