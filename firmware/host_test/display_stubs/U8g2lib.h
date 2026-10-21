// Models many transfers per U8g2 call so one bus timeout cannot multiply.
#pragma once
#include <stdint.h>
#include <string>
#include <vector>
#define U8G2_R0 0
#define U8X8_PIN_NONE 255
enum { U8X8_MSG_BYTE_INIT, U8X8_MSG_BYTE_SET_DC, U8X8_MSG_BYTE_START_TRANSFER,
       U8X8_MSG_BYTE_SEND, U8X8_MSG_BYTE_END_TRANSFER };
struct u8x8_t {
    uint8_t (*byte_cb)(u8x8_t*, uint8_t, uint8_t, void*) = nullptr;
};
static const uint8_t u8g2_font_5x8_tf[] = {0};
static const uint8_t u8g2_font_logisoso26_tr[] = {1};
static const uint8_t u8g2_font_helvB14_tr[] = {2};
static const uint8_t u8g2_font_logisoso38_tr[] = {3};
struct U8G2_SH1106_128X64_NONAME_F_HW_I2C {
    u8x8_t bus;
    unsigned sleeps = 0, wakes = 0, frames = 0;
    const uint8_t* font = nullptr;
    bool baseline = false;
    struct Text { unsigned x, y; std::string value; const uint8_t* font; };
    std::vector<Text> text;
    U8G2_SH1106_128X64_NONAME_F_HW_I2C(int, int) {}
    u8x8_t* getU8x8() { return &bus; }
    void transfers() {
        uint8_t bytes[24] = {};
        for (unsigned i = 0; i < 50; ++i) {
            bus.byte_cb(&bus, U8X8_MSG_BYTE_START_TRANSFER, 0, nullptr);
            bus.byte_cb(&bus, U8X8_MSG_BYTE_SEND, sizeof bytes, bytes);
            bus.byte_cb(&bus, U8X8_MSG_BYTE_END_TRANSFER, 0, nullptr);
        }
    }
    void begin() { transfers(); }
    void initDisplay() { transfers(); }
    void setI2CAddress(int) {}
    void setBusClock(int) {}
    void setContrast(int) { transfers(); }
    void setFont(const uint8_t* value) { font = value; }
    void setFontPosTop() {}
    void setFontPosBaseline() { baseline = true; }
    void clearBuffer() { text.clear(); }
    void drawStr(unsigned x, unsigned y, const char* value) { text.push_back({x, y, value, font}); }
    void sendBuffer() { ++frames; transfers(); }
    void setPowerSave(int off) { sleeps += off; wakes += !off; transfers(); }
};
