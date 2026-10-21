// Bus fault injection against the actual OLED callback/disable policy.
#include <cassert>
#include <cstdio>
struct DisplaySerial {
    unsigned warnings = 0;
    void println(const char*) { ++warnings; }
} Serial;
#include "debug_display.h"

static void reset() {
    gDebugDisplayBegun = false;
    Wire = DisplayTestWire{};
    gDebugDisplayFailed = gDebugDisplayReady = false;
    Serial.warnings = 0;
}

int main() {
    reset();
    Wire.failAt = 1;  // missing OLED: probe fails before U8g2 init
    beginDebugDisplay();
    assert(!gDebugDisplayReady && Serial.warnings == 1 && Wire.ends == 1);
    drawDebugDisplay(DebugStatus{});
    debugDisplayPowerSave(true);
    assert(Wire.ends == 1 && Serial.warnings == 1);

    reset();
    Wire.failAt = 3;  // hung bus midway through a 50-transfer init
    beginDebugDisplay();
    assert(!gDebugDisplayReady && Serial.warnings == 1 && Wire.ends == 3);

    reset();
    beginDebugDisplay();
    assert(gDebugDisplayReady && !Serial.warnings);
    assert(Wire.sda == 17 && Wire.scl == 18 && Wire.clock == 400000);
    assert(Wire.timeout == 10 && Wire.address == 0x3c);
    debugDisplayPowerSave(true);
    assert(gDebugOled.sleeps == 1);
    debugDisplayPowerSave(false);
    unsigned before = Wire.ends;
    Wire.failAt = before + 2;  // disconnect halfway through a frame
    drawDebugDisplay(DebugStatus{});
    assert(Wire.ends == before + 2 && !gDebugDisplayReady && Serial.warnings == 1);
    for (unsigned i = 0; i < 5; ++i) drawDebugDisplay(DebugStatus{});
    assert(Wire.ends == before + 2 && Serial.warnings == 1);

    reset();
    Wire.beginOk = false;
    beginDebugDisplay();
    assert(!Wire.starts && !gDebugDisplayReady && Serial.warnings == 1);

    reset();
    beginDebugDisplay();
    before = Wire.ends;
    Wire.writeOk = false;
    drawDebugDisplay(DebugStatus{});
    assert(Wire.ends == before + 1 && Wire.starts == Wire.ends);
    assert(!gDebugDisplayReady && Serial.warnings == 1); // write lock released
    puts("Debug OLED missing/init/frame fault tests passed.");
}
