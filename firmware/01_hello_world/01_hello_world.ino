// =================================================================
// Stage 1: Hello World / Heartbeat Test
// =================================================================
// Purpose: Verify that your entire development toolchain works —
// the CH9102 driver, Arduino IDE, ESP32 board support, compiler,
// uploader, and serial communication back from the board to your PC.
//
// What to expect:
//   - Open Serial Monitor (Tools → Serial Monitor) at 115200 baud
//   - You should see "T-Beam is alive!" followed by heartbeat messages
//   - If you see garbled text, check the baud rate dropdown in Serial Monitor
//
// Upload instructions:
//   1. Tools → Board → esp32 → ESP32 Dev Module
//   2. Tools → Port → (your COM port, e.g., COM5)
//   3. Tools → Upload Speed → 921600
//   4. Click the Upload arrow button
//   5. If "Connecting..." hangs, see firmware/README.md, "If uploading fails".
//      T-Beam V1.2 has PWR, IO38 and RST; IO38 is NOT a GPIO 0 BOOT button.
// =================================================================

void setup() {
    // Start the serial connection at 115200 baud. This is the speed
    // at which the ESP32 will send text back to your computer for
    // display in the Serial Monitor.
    Serial.begin(115200);
    delay(2000);  // Give the serial port time to initialize

    // Print a startup message so we know the board booted
    Serial.println("========================================");
    Serial.println("  T-Beam is alive!");
    Serial.println("  Stage 1: Hello World Test");
    Serial.println("========================================");
    Serial.println("If you can read this, your setup is working.");
    Serial.println();
}

void loop() {
    // Print a heartbeat message every 2 seconds so we can see
    // the board is running continuously, not just booting once.
    // The millis() value tells us how long the board has been
    // running, which is useful for confirming it hasn't rebooted.
    Serial.print("Heartbeat - T-Beam running for ");
    Serial.print(millis() / 1000);
    Serial.println(" seconds");
    delay(2000);
}
