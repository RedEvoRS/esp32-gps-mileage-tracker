#include "production_display_layout.h"
#include <cassert>
#include <cstdio>
#include <cstring>
using namespace display;
static unsigned cases = 0;

static void fits(const Layout& pixels) {
    for (unsigned i = 0; i < pixels.count; ++i) {
        const auto& t = pixels.text[i];
        for (const char* ch = t.value; *ch; ++ch) assert(std::strchr(kGlyphChars, *ch));
        auto b = textBounds(t.font, t.value);
        assert(t.x + b.left >= kMargin && t.x + b.right <= kPanelWidth - kMargin);
        assert(t.baseline + b.top >= kMargin && t.baseline + b.bottom <= kPanelHeight - kMargin);
        for (unsigned j = 0; j < i; ++j) {
            const auto& other = pixels.text[j];
            auto a = textBounds(other.font, other.value);
            // At least one blank pixel between every pair of ink rectangles.
            assert(t.x + b.left > other.x + a.right || other.x + a.left > t.x + b.right ||
                   t.baseline + b.top > other.baseline + a.bottom ||
                   other.baseline + a.top > t.baseline + b.bottom);
        }
    }
    ++cases;
}
int main() {
    assert(layout({}).count == 0);
    const auto off = layout(switchingOffCard()); fits(off);
    assert(off.count == 1 && off.text[0].font == Font::Codes);
    assert(!std::strcmp(off.text[0].value, "Switching off"));
    const auto usb = layout(usbPowerCard()); fits(usb);
    assert(usb.count == 3);
    const char* usbText[] = {"USB power", "Unplug USB", "to switch off"};
    for (unsigned i = 0; i < 3; ++i) {
        assert(!std::strcmp(usb.text[i].value, usbText[i]));
        assert(usb.text[i].font == Font::Codes);
    }
    auto pixels = layout(statusCard(99, nullptr, 0));
    fits(pixels);
    assert(pixels.count == 2 && !std::strcmp(pixels.text[0].value, "Sats: 99"));
    assert(!std::strcmp(pixels.text[1].value, "OK"));
    assert(pixels.text[0].x == 2 && pixels.text[0].baseline == 29);
    assert(pixels.text[1].x == 47 && pixels.text[1].baseline == 61);
    auto sats = textBounds(Font::Sats, "Sats: 99");
    assert(sats.advance == 123 && sats.right - sats.left == 119 && sats.bottom - sats.top == 27);
    auto code = textBounds(Font::Codes, "E9999");
    assert(code.advance == 53 && code.right - code.left == 50 && code.bottom - code.top == 14);
    auto error = textBounds(Font::Error, "E9999");
    assert(error.advance == 121 && error.right - error.left == 116 && error.bottom - error.top == 38);
    pixels = layout(statusCard(-1, nullptr, 0)); fits(pixels);
    assert(!std::strcmp(pixels.text[0].value, "Sats: --"));
    // Preserve the real heard count above two digits using the smaller font.
    pixels = layout(statusCard(255, nullptr, 0)); fits(pixels);
    assert(!std::strcmp(pixels.text[0].value, "Sats: 255") && pixels.text[0].font == Font::Codes);

    Condition faults[64];
    for (unsigned i = 0; i < 64; ++i) faults[i] = {uint16_t(9900+i), i+1, 0};
    for (unsigned count = 1; count <= 64; ++count) {
        pixels = layout(statusCard(99, faults, count)); fits(pixels);
        assert(pixels.count == 1 + (count < kCodeSlots ? count : kCodeSlots));
        unsigned visible = count <= kCodeSlots ? count : kCodeSlots-1;
        assert(pixels.overflow == count - visible);
        for (unsigned i = 0; i < visible; ++i) {
            char expected[12]; std::snprintf(expected, sizeof expected, "E%04u", unsigned(faults[i].code));
            assert(!std::strcmp(pixels.text[i+1].value, expected));
        }
        if (count > kCodeSlots) {
            char expected[12]; std::snprintf(expected, sizeof expected, "+%u", count-visible);
            assert(!std::strcmp(pixels.text[4].value, expected));
        }
    }
    assert(!std::strcmp(pixels.text[4].value, "+61"));
    assert(pixels.text[1].baseline == 46 && pixels.text[2].baseline == 46);
    assert(pixels.text[3].baseline == 62 && pixels.text[4].baseline == 62);
    // Worst-case token extents, including the complete two-digit overflow range.
    for (unsigned n = 1; n <= 99; ++n) {
        char value[12]; std::snprintf(value, sizeof value, "+%u", n);
        auto b = textBounds(Font::Codes, value);
        assert(b.advance <= 31 && b.right - b.left <= 29 && b.bottom - b.top <= 13);
    }
    // Every four-digit value fits, including glyph 3's below-baseline pixel.
    for (unsigned n = 0; n <= 9999; ++n) {
        fits(layout(errorCard(uint16_t(n))));
        Condition one{uint16_t(n), 1, 0};
        fits(layout(statusCard(int(n % 100), &one, 1)));
        auto card = statusCard(int(n % 100), faults, 4);
        for (auto& c : card.codes) c = uint16_t(n);
        fits(layout(card));
    }
    pixels = layout(errorCard(9999)); fits(pixels);
    assert(pixels.count == 1 && !std::strcmp(pixels.text[0].value, "E9999"));
    assert(pixels.text[0].x == 3 && pixels.text[0].baseline == 51);
    pixels = layout(errorCard(6999)); fits(pixels); // unknown code, no description
    assert(pixels.count == 1 && !std::strcmp(pixels.text[0].value, "E6999"));
    std::printf("test_production_display_layout: %u layouts passed (real U8g2 metrics)\n", cases);
}
