// Models many transfers per U8g2 call so one bus timeout cannot multiply.
#pragma once
#include <stdint.h>
#define U8G2_R0 0
#define U8X8_PIN_NONE 255
enum { U8X8_MSG_BYTE_INIT, U8X8_MSG_BYTE_SET_DC, U8X8_MSG_BYTE_START_TRANSFER,
       U8X8_MSG_BYTE_SEND, U8X8_MSG_BYTE_END_TRANSFER };
struct u8x8_t {
    uint8_t (*byte_cb)(u8x8_t*, uint8_t, uint8_t, void*) = nullptr;
};
static const uint8_t u8g2_font_5x8_tf[] = {0};
struct U8G2_SH1106_128X64_NONAME_F_HW_I2C {
    u8x8_t bus;
    unsigned sleeps = 0;
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
    void setFont(const uint8_t*) {}
    void setFontPosTop() {}
    void clearBuffer() {}
    void drawStr(unsigned, unsigned, const char*) {}
    void sendBuffer() { transfers(); }
    void setPowerSave(int off) { sleeps += off; transfers(); }
};
