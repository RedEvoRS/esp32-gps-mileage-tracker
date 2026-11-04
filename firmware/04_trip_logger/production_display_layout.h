// Pure pixel layout, using glyph metrics extracted from the pinned U8g2 fonts.
#pragma once
#include <cstdio>
#include "display_model.h"
#include "display_font_metrics.h"

namespace display {
constexpr int kPanelWidth = 128, kPanelHeight = 64, kMargin = 2;
constexpr unsigned kCodeColumns = 2, kCodeRows = 2, kCodeSlots = 4;
struct Bounds { int left = 0, top = 0, right = 0, bottom = 0, advance = 0; };
inline Bounds textBounds(Font font, const char* text) {
    Bounds out; bool ink = false;
    for (; *text; ++text) {
        size_t i = 0;
        while (kGlyphChars[i] && kGlyphChars[i] != *text) ++i;
        if (!kGlyphChars[i]) continue; // only the bounded card alphabet is used
        const auto& g = kGlyphMetrics[unsigned(font)][i];
        if (g.width && g.height) {
            int left = out.advance + g.x, right = left + g.width;
            int top = -g.height - g.y, bottom = -g.y;
            if (!ink || left < out.left) out.left = left;
            if (!ink || right > out.right) out.right = right;
            if (!ink || top < out.top) out.top = top;
            if (!ink || bottom > out.bottom) out.bottom = bottom;
            ink = true;
        }
        out.advance += g.advance;
    }
    return out; // right/bottom are exclusive
}
struct Text {
    Font font = Font::Codes;
    int x = 0, baseline = 0;
    char value[24] = {};
};
struct Layout {
    Text text[1 + kCodeSlots] = {};
    unsigned count = 0, overflow = 0;
};
inline void centre(Text& text, int left, int width) {
    auto bounds = textBounds(text.font, text.value);
    text.x = left + (width - (bounds.right - bounds.left)) / 2 - bounds.left;
}
inline Layout layout(const Output& out) {
    Layout result;
    if (!out.powered || out.card == Card::Blank) return result;
    if (out.card == Card::SwitchingOffCard) {
        auto& text = result.text[result.count++];
        std::snprintf(text.value, sizeof text.value, "Switching off");
        auto bounds = textBounds(text.font, text.value);
        text.baseline = (kPanelHeight - (bounds.bottom - bounds.top)) / 2 - bounds.top;
        centre(text, 0, kPanelWidth);
        return result;
    }
    if (out.card == Card::UsbPowerCard) {
        const char* lines[] = {"USB power", "Unplug USB", "to switch off"};
        for (unsigned i = 0; i < 3; ++i) {
            auto& text = result.text[result.count++];
            std::snprintf(text.value, sizeof text.value, "%s", lines[i]);
            text.baseline = 18 + int(i) * 21;
            centre(text, 0, kPanelWidth);
        }
        return result;
    }
    auto& heading = result.text[result.count++];
    if (out.card == Card::ErrorCard) {
        heading.font = Font::Error;
        std::snprintf(heading.value, sizeof heading.value, "E%04u", unsigned(out.errorCode));
        // Protocol codes have four digits; preserve larger defensive inputs too.
        auto bounds = textBounds(heading.font, heading.value);
        if (bounds.right - bounds.left > kPanelWidth - 2*kMargin) heading.font = Font::Sats;
        bounds = textBounds(heading.font, heading.value);
        heading.baseline = (kPanelHeight - (bounds.bottom - bounds.top)) / 2 - bounds.top;
        centre(heading, 0, kPanelWidth);
        return result;
    }
    heading.font = Font::Sats;
    if (out.heard < 0) std::snprintf(heading.value, sizeof heading.value, "Sats: --");
    else std::snprintf(heading.value, sizeof heading.value, "Sats: %d", out.heard);
    // GSV can represent more than 99: retain the value with the smaller font.
    auto bounds = textBounds(heading.font, heading.value);
    if (bounds.right - bounds.left > kPanelWidth - 2*kMargin) heading.font = Font::Codes;
    heading.baseline = 29; // worst sats ink y=2..29 (exclusive bottom 30)
    centre(heading, 0, kPanelWidth);
    if (!out.codeCount) {
        auto& ok = result.text[result.count++];
        ok.font = Font::Sats;
        std::snprintf(ok.value, sizeof ok.value, "OK");
        ok.baseline = 61; // ink y=35..60, below the sats line
        centre(ok, 0, kPanelWidth);
        return result;
    }
    unsigned shown = out.codeCount > kCodeSlots ? kCodeSlots - 1 : unsigned(out.codeCount);
    result.overflow = unsigned(out.codeCount) - shown;
    unsigned tokens = shown + (result.overflow ? 1 : 0);
    for (unsigned i = 0; i < tokens; ++i) {
        auto& token = result.text[result.count++];
        if (i < shown) std::snprintf(token.value, sizeof token.value, "E%04u", unsigned(out.codes[i]));
        else std::snprintf(token.value, sizeof token.value, "+%u", result.overflow);
        if (tokens == 1) { token.font = Font::Sats; token.baseline = 61; }
        else token.baseline = tokens <= kCodeColumns ? 55 : 46 + int(i / kCodeColumns) * 16;
        // A single code uses the full row; otherwise two 62-pixel cells.
        int width = tokens == 1 ? 124 : 62;
        centre(token, kMargin + int(i % kCodeColumns) * 62, width);
    }
    return result;
}
} // namespace display
