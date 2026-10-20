#include <cassert>
#include <cstdio>
#include "production_display.h"
static unsigned cases = 0;
static void reset() {
    Wire = DisplayTestWire{};
    gProductionDisplayBegun = gProductionDisplayReady = gProductionDisplayFailed = false;
    gProductionHaveOutput = false;
    gProductionOled = U8G2_SH1106_128X64_NONAME_F_HW_I2C(U8G2_R0, U8X8_PIN_NONE);
}
static void stopped() {
    assert(gProductionDisplayFailed && !gProductionDisplayReady);
    unsigned ends = Wire.ends;
    for (int i = 0; i < 100; ++i) {
        beginProductionDisplay(); drawProductionDisplay(display::errorCard(4003)); productionDisplayPowerSave(true);
    }
    assert(Wire.ends == ends); ++cases;
}
int main() {
    reset(); Wire.beginOk = false; beginProductionDisplay(); assert(!Wire.starts); stopped();
    reset(); Wire.failAt = 1; beginProductionDisplay(); assert(Wire.ends == 1); stopped();
    reset(); Wire.failAt = 3; beginProductionDisplay(); assert(Wire.ends == 3); stopped();
    reset(); beginProductionDisplay();
    assert(gProductionDisplayReady && gProductionOled.sleeps == 1);
    assert(Wire.timeout == 10 && Wire.sda == 17 && Wire.scl == 18 && Wire.address == 0x3c);
    auto card = display::statusCard(12, nullptr, 0);
    drawProductionDisplay(card); unsigned ends = Wire.ends;
    for (unsigned i = 0; i < 100; ++i) { card.until = i; drawProductionDisplay(card); }
    assert(Wire.ends == ends); ++cases;
    drawProductionDisplay({}); assert(gProductionOled.sleeps == 2);
    ends = Wire.ends; drawProductionDisplay({}); assert(Wire.ends == ends); ++cases;
    Wire.failAt = ends + 2; drawProductionDisplay(card);
    assert(Wire.ends == ends + 2); stopped();
    reset(); beginProductionDisplay(); ends = Wire.ends; Wire.writeOk = false;
    drawProductionDisplay(card); assert(Wire.ends == ends + 1 && Wire.starts == Wire.ends); stopped();
    std::printf("test_production_display: %u scenarios passed\n", cases);
}
