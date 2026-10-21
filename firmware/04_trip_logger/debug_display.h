// SH1106 adapter, owned entirely by core 1. No changes to PMU supply rails.
#pragma once
#include <U8g2lib.h>
#include <Wire.h>
#include "debug_status.h"

// Raised from 16: unreadable outdoors on the first road test (2026-10-19).
// Full contrast speeds OLED burn-in and draws a little more current;
// acceptable for this shakedown-only page, which shifts horizontally each minute.
static constexpr uint8_t DEBUG_OLED_CONTRAST = 255;  // SH1106 maximum

static U8G2_SH1106_128X64_NONAME_F_HW_I2C gDebugOled(U8G2_R0, U8X8_PIN_NONE);
static bool gDebugDisplayReady = false, gDebugDisplayFailed = false;
static bool gDebugWireReady = false; // also permits BME280 when only OLED is absent
static bool gDebugDisplayBegun = false;
static uint32_t gDebugDrawMs = 0;

// U8g2's stock Arduino callback ignores endTransmission errors. Latch the
// first failure and skip ALL remaining transfers in that frame/init sequence:
// a wedged bus costs one 10 ms timeout, not one timeout per tile. Normal
// full-frame traffic at 400 kHz is ~25-35 ms, well below the 2 s UART budget.
static uint8_t debugOledByte(u8x8_t*, uint8_t msg, uint8_t len, void* data) {
    if (gDebugDisplayFailed) return 0;
    switch (msg) {
    case U8X8_MSG_BYTE_INIT: return 1;  // Wire already configured, not Wire1
    case U8X8_MSG_BYTE_SET_DC: return 1;
    case U8X8_MSG_BYTE_START_TRANSFER:
        Wire.beginTransmission(0x3C);
        return 1;
    case U8X8_MSG_BYTE_SEND:
        if (Wire.write((const uint8_t*)data, len) != len) {
            // Release Wire's transaction lock even on a short buffered write.
            Wire.endTransmission();
            gDebugDisplayFailed = true;
        }
        return !gDebugDisplayFailed;
    case U8X8_MSG_BYTE_END_TRANSFER:
        if (Wire.endTransmission() != 0) gDebugDisplayFailed = true;
        return !gDebugDisplayFailed;
    default: return 0;
    }
}

static void debugDisplayCheck() {
    if (gDebugDisplayFailed && gDebugDisplayReady) {
        gDebugDisplayReady = false;
        Serial.println("WARNING: debug OLED I2C failed; display disabled for this boot.");
    }
}

static void beginDebugDisplay(bool wireAlreadyReady = false) {
    if (gDebugDisplayBegun) return; // never reinitialize/revive after a failure
    gDebugDisplayBegun = true;
    gDebugDisplayReady = true;  // also makes an init failure log exactly once
    Wire.setTimeOut(10);
    gDebugWireReady = wireAlreadyReady || Wire.begin(17, 18, 400000);
    if (!gDebugWireReady) gDebugDisplayFailed = true;
    if (!gDebugDisplayFailed) {
        Wire.beginTransmission(0x3C);
        if (Wire.endTransmission() != 0) gDebugDisplayFailed = true;
    }
    if (!gDebugDisplayFailed) {
        gDebugOled.getU8x8()->byte_cb = debugOledByte;
        gDebugOled.setI2CAddress(0x3C << 1);  // U8g2 takes an 8-bit address
        gDebugOled.setBusClock(400000);
        gDebugOled.initDisplay(); // asleep until the owner has drawn fresh pixels
        gDebugOled.setContrast(DEBUG_OLED_CONTRAST);
        gDebugOled.setFont(u8g2_font_5x8_tf);  // 21 chars = 105 px; 8 rows
        gDebugOled.setFontPosTop();
    }
    debugDisplayCheck();
}

static void debugDisplayPowerSave(bool off) {
    if (!gDebugDisplayReady) return;
    gDebugOled.setPowerSave(off ? 1 : 0);
    debugDisplayCheck();
}

static void drawDebugDisplay(const DebugStatus& status) {
    if (!gDebugDisplayReady) return;
    char lines[8][22];
    formatDebugStatus(status, lines);
    gDebugOled.clearBuffer();
    // Horizontal shift only: eight 8px rows use the full display height.
    unsigned x = (status.now / 60000 % 3) * 2;
    for (unsigned row = 0; row < 8; ++row) gDebugOled.drawStr(x, row * 8, lines[row]);
    gDebugOled.sendBuffer();
    debugDisplayCheck();
}
