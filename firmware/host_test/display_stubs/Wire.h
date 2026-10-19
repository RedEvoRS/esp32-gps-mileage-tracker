// Isolated display transport fault injection; never used by other host tests.
#pragma once
#include <stdint.h>
#include <stddef.h>
struct DisplayTestWire {
    bool beginOk = true, writeOk = true;
    unsigned timeout = 0, clock = 0, address = 0, starts = 0, ends = 0;
    int sda = -1, scl = -1;
    unsigned failAt = 0;
    void setTimeOut(unsigned ms) { timeout = ms; }
    bool begin(int a, int b, unsigned hz) { sda = a; scl = b; clock = hz; return beginOk; }
    void beginTransmission(unsigned addr) { address = addr; ++starts; }
    size_t write(const uint8_t*, size_t len) { return writeOk ? len : 0; }
    unsigned endTransmission() { return ++ends == failAt ? 5 : 0; }
};
static DisplayTestWire Wire;
