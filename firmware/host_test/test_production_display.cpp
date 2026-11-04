#include <cassert>
#include <cstdio>
#define FIRMWARE_VERSION_BASE "v1234567890123456789012345678901"
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
    drawDebugModeFeedback(true);
    assert(gProductionOled.text.size() == 3);
    assert(gProductionOled.text[0].value == "Debug ON");
    assert(gProductionOled.text[0].font == u8g2_font_helvB14_tr);
    const auto& first = gProductionOled.text[1];
    const auto& second = gProductionOled.text[2];
    assert(first.value.size() == 21 && second.value.size() == 11);
    assert(first.value + second.value == firmware::kVersion);
    assert(first.x == 8 && first.y == 52 && second.x == 8 && second.y == 62);
    assert(first.font == u8g2_font_5x8_tf && second.font == u8g2_font_5x8_tf);
    drawDebugModeFeedback(false);
    assert(gProductionOled.text.size() == 1 && gProductionOled.text[0].value == "Debug OFF");
    ++cases;
    auto card = display::statusCard(12, nullptr, 0);
    drawProductionDisplay(card); unsigned ends = Wire.ends;
    assert(gProductionOled.baseline && gProductionOled.text.size() == 2);
    assert(gProductionOled.text[0].value == "Sats: 12" && gProductionOled.text[1].value == "OK");
    assert(gProductionOled.text[0].font == u8g2_font_logisoso26_tr);
    assert(gProductionOled.text[1].font == u8g2_font_logisoso26_tr);
    for (unsigned i = 0; i < 100; ++i) { card.until = i; drawProductionDisplay(card); }
    assert(Wire.ends == ends); ++cases;
    drawProductionDisplay({}); assert(gProductionOled.sleeps == 2);
    ends = Wire.ends; drawProductionDisplay({}); assert(Wire.ends == ends); ++cases;
    drawProductionDisplay(display::errorCard(9999));
    assert(gProductionOled.text.size() == 1 && gProductionOled.text[0].value == "E9999");
    assert(gProductionOled.text[0].font == u8g2_font_logisoso38_tr);
    assert(gProductionOled.text[0].x == 3 && gProductionOled.text[0].y == 51); ++cases;
    display::Condition many[64];
    for (unsigned i = 0; i < 64; ++i) many[i] = {uint16_t(9900+i), i+1, 0};
    drawProductionDisplay(display::statusCard(-1, many, 64));
    assert(gProductionOled.text.size() == 5 && gProductionOled.text[0].value == "Sats: --");
    assert(gProductionOled.text[1].value == "E9900" && gProductionOled.text[4].value == "+61");
    for (unsigned i = 1; i < 5; ++i) assert(gProductionOled.text[i].font == u8g2_font_helvB14_tr);
    ++cases;
    ends = Wire.ends;
    drawProductionDisplay(display::usbPowerCard());
    assert(gProductionOled.text.size() == 3);
    assert(gProductionOled.text[0].value == "USB power");
    assert(gProductionOled.text[1].value == "Unplug USB");
    assert(gProductionOled.text[2].value == "to switch off");
    for (const auto& line : gProductionOled.text) assert(line.font == u8g2_font_helvB14_tr);
    ends = Wire.ends;
    drawProductionDisplay(display::usbPowerCard()); assert(Wire.ends == ends); ++cases;
    Wire.failAt = ends + 2; drawProductionDisplay(card);
    assert(Wire.ends == ends + 2); stopped();
    reset(); beginProductionDisplay(); ends = Wire.ends; Wire.writeOk = false;
    drawProductionDisplay(card); assert(Wire.ends == ends + 1 && Wire.starts == Wire.ends); stopped();
    std::printf("test_production_display: %u scenarios passed\n", cases);
}
