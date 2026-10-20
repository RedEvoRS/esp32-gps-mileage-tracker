// SH1106 adapter, owned entirely by core 1. No changes to PMU supply rails.
#pragma once
#include <U8g2lib.h>
#include <Wire.h>
#include "display_model.h"

static constexpr uint8_t PRODUCTION_OLED_CONTRAST = 255;  // SH1106 maximum

static U8G2_SH1106_128X64_NONAME_F_HW_I2C gProductionOled(U8G2_R0, U8X8_PIN_NONE);
static bool gProductionDisplayReady = false, gProductionDisplayFailed = false;
static bool gProductionDisplayBegun = false;
static display::Output gProductionLastOutput;
static bool gProductionHaveOutput = false;

// U8g2's stock Arduino callback ignores endTransmission errors. Latch the
// first failure and skip ALL remaining transfers in that frame/init sequence:
// a wedged bus costs one 10 ms timeout, not one timeout per tile. Normal
// full-frame traffic at 400 kHz is ~25-35 ms, well below the 2 s UART budget.
static uint8_t productionOledByte(u8x8_t*, uint8_t msg, uint8_t len, void* data) {
    if (gProductionDisplayFailed) return 0;
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
            gProductionDisplayFailed = true;
        }
        return !gProductionDisplayFailed;
    case U8X8_MSG_BYTE_END_TRANSFER:
        if (Wire.endTransmission() != 0) gProductionDisplayFailed = true;
        return !gProductionDisplayFailed;
    default: return 0;
    }
}

static void productionDisplayCheck() {
    if (gProductionDisplayFailed && gProductionDisplayReady) {
        gProductionDisplayReady = false;
        // E6001 is published by the normal core-1 diagnostic hook.
    }
}

static void beginProductionDisplay() {
    if (gProductionDisplayBegun) return; // no retries, even after init failure
    gProductionDisplayBegun = true;
    gProductionDisplayReady = true;
    Wire.setTimeOut(10);
    if (!Wire.begin(17, 18, 400000)) gProductionDisplayFailed = true;
    if (!gProductionDisplayFailed) {
        Wire.beginTransmission(0x3C);
        if (Wire.endTransmission() != 0) gProductionDisplayFailed = true;
    }
    if (!gProductionDisplayFailed) {
        gProductionOled.getU8x8()->byte_cb = productionOledByte;
        gProductionOled.setI2CAddress(0x3C << 1);  // U8g2 takes an 8-bit address
        gProductionOled.setBusClock(400000);
        // initDisplay leaves the SH1106 in sleep; begin() would wake it.
        gProductionOled.initDisplay();
        gProductionOled.setPowerSave(1);
        gProductionOled.setContrast(PRODUCTION_OLED_CONTRAST);
        gProductionOled.setFont(u8g2_font_5x8_tf);  // 21 chars = 105 px; 8 rows
        gProductionOled.setFontPosTop();
    }
    productionDisplayCheck();
}

static void productionDisplayPowerSave(bool off) {
    if (!gProductionDisplayReady) return;
    gProductionOled.setPowerSave(off ? 1 : 0);
    productionDisplayCheck();
}

// Stable cards are sent once. Deadline changes alone do not redraw their pixels.
static void drawProductionDisplay(const display::Output& out) {
    if (!gProductionDisplayReady) return;
    if (gProductionHaveOutput && out.powered == gProductionLastOutput.powered &&
        out.card == gProductionLastOutput.card &&
        std::memcmp(out.lines, gProductionLastOutput.lines, sizeof out.lines) == 0) return;
    bool wasPowered = gProductionHaveOutput && gProductionLastOutput.powered;
    gProductionLastOutput = out; gProductionHaveOutput = true;
    if (out.powered) {
        gProductionOled.clearBuffer();
        for (unsigned row = 0; row < 8; ++row) gProductionOled.drawStr(0, row * 8, out.lines[row]);
        gProductionOled.sendBuffer();
        // Upload the new pixels before waking: no stale-card flash.
        if (!wasPowered) productionDisplayPowerSave(false);
    } else if (wasPowered) productionDisplayPowerSave(true);
    productionDisplayCheck();
}
