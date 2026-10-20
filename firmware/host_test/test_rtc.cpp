#include "rtc_pcf8563.h"
#include "diagnostic_clock.h"
#include <cassert>
#include <cstdio>
#include <string>
#include <initializer_list>
using namespace diagnostics;
static unsigned checks = 0;
#define CHECK(v) do { ++checks; assert(v); } while (0)
int main() {
    unsigned n;
    for (unsigned i = 0; i < 100; ++i) {
        CHECK(rtc::decodeBcd(rtc::encodeBcd(i), n) && n == i);
    }
    CHECK(!rtc::decodeBcd(0x1a, n)); CHECK(!rtc::decodeBcd(0xa1, n));
    uint8_t registers[7]; uint32_t utc = 0;
    CHECK(rtc::toUtc({2026,1,1,0,0,0}) == rtc::kFirstUtc);
    CHECK(rtc::toUtc({2099,12,31,23,59,59}) == rtc::kLastUtc);
    // Every day of the entire supported range, at both ends of the day.
    for (uint32_t day = rtc::kFirstUtc; day <= rtc::kLastUtc - 86399; day += 86400) {
        for (unsigned second : {0u, 1u, 43200u, 86399u}) {
            uint32_t t = day + second;
            CHECK(rtc::toUtc(rtc::fromUtc(t)) == t);
            CHECK(rtc::encode(t, registers));
            CHECK(rtc::validate(0, registers, utc) == rtc::Status::Valid && utc == t);
        }
    }
    CHECK(!rtc::encode(rtc::kFirstUtc - 1, registers));
    CHECK(!rtc::encode(rtc::kLastUtc + 1, registers));
    rtc::encode(rtc::toUtc({2028,2,29,23,59,59}), registers);
    CHECK(rtc::validate(0, registers, utc) == rtc::Status::Valid);
    registers[6] = 0x27;
    CHECK(rtc::validate(0, registers, utc) == rtc::Status::InvalidDate);
    registers[6] = 0x28; registers[5] |= 0x80;
    CHECK(rtc::validate(0, registers, utc) == rtc::Status::OutOfRange);
    rtc::encode(rtc::kFirstUtc, registers); registers[6] = 0x25;
    CHECK(rtc::validate(0, registers, utc) == rtc::Status::OutOfRange);
    for (auto field : {0,1,2,3,5,6}) {
        rtc::encode(rtc::kFirstUtc, registers); registers[field] = 0x1a;
        CHECK(rtc::validate(0, registers, utc) == rtc::Status::InvalidDate);
    }
    const uint8_t limits[] = {0x60,0x60,0x24,0x00,0xff,0x00,0x26};
    for (auto field : {0,1,2,3,5}) {
        rtc::encode(rtc::kFirstUtc, registers); registers[field] = limits[field];
        CHECK(rtc::validate(0, registers, utc) == rtc::Status::InvalidDate);
    }
    rtc::encode(rtc::kFirstUtc, registers); registers[3] = 0x32;
    CHECK(rtc::validate(0, registers, utc) == rtc::Status::InvalidDate);
    registers[3] = 1; registers[5] = 0x13;
    CHECK(rtc::validate(0, registers, utc) == rtc::Status::InvalidDate);
    registers[5] = 4; registers[3] = 0x31;
    CHECK(rtc::validate(0, registers, utc) == rtc::Status::InvalidDate);
    rtc::encode(rtc::kFirstUtc, registers); registers[4] = 0xff;
    CHECK(rtc::validate(0, registers, utc) == rtc::Status::Valid); // weekday ignored
    registers[0] = 0xff;
    CHECK(rtc::validate(0, registers, utc) == rtc::Status::Stopped); // VL first
    registers[0] = 0x6a;
    CHECK(rtc::validate(0x20, registers, utc) == rtc::Status::Stopped); // STOP first
    CHECK(rtc::leap(2000) && !rtc::leap(1900) && !rtc::leap(2100));
    CHECK(rtc::aligned(1000) && rtc::aligned(1100) && !rtc::aligned(1101) && !rtc::aligned(1999));
    CHECK(!rtc::needsWrite(true,100,100) && !rtc::needsWrite(true,101,100));
    CHECK(rtc::needsWrite(true,102,100) && rtc::needsWrite(true,98,100) && rtc::needsWrite(false,100,100));
    CHECK(std::string(rtc::reason(rtc::Status::ReadFailed)) == "read_failed");
    DiagnosticClock clock; EventInput e;
    clock.tick(UINT32_MAX - 50);
    clock.fill(e,UINT32_MAX - 50,false,0);
    CHECK(e.timeSource == EventTimeSource::Unknown && e.utcMs == -1 && e.syncAgeMs == -1);
    clock.rtcAnchor(rtc::kFirstUtc,UINT32_MAX - 50,-1);
    clock.fill(e,49,false,0); // reader crosses wrap before the next loop tick
    CHECK(e.utcMs == int64_t(rtc::kFirstUtc)*1000+100);
    clock.tick(49); clock.fill(e,49,false,0);
    CHECK(e.timeSource == EventTimeSource::Rtc && e.utcMs == int64_t(rtc::kFirstUtc)*1000+100 && e.syncAgeMs == -1);
    clock.fill(e,UINT32_MAX - 25,false,0); // sampled just before the published wrap
    CHECK(e.utcMs == int64_t(rtc::kFirstUtc)*1000+25);
    clock.rtcAnchor(rtc::kFirstUtc,49,5000);
    clock.trusted(EventTimeSource::Gps,49); clock.fill(e,50,true,123456);
    CHECK(e.timeSource == EventTimeSource::Gps && e.syncAgeMs == 1 && e.utcMs == 123456);
    clock.trusted(EventTimeSource::Sntp,51); clock.fill(e,52,true,123457);
    CHECK(e.timeSource == EventTimeSource::Sntp && e.syncAgeMs == 1);
    clock.trusted(EventTimeSource::Gps,53); clock.fill(e,54,true,123458);
    CHECK(e.timeSource == EventTimeSource::Gps && e.syncAgeMs == 1);
    clock.trusted(EventTimeSource::Sntp,53); clock.fill(e,54,true,123458);
    CHECK(e.timeSource == EventTimeSource::Sntp && e.syncAgeMs == 1); // same-ms handover
    clock.trusted(EventTimeSource::Gps,53);
    clock.fill(e,54,false,0);
    CHECK(e.timeSource == EventTimeSource::Rtc && e.syncAgeMs == 5005);
    // Continue across a second full millis period, with loop servicing ticks.
    clock.tick(UINT32_MAX - 50); clock.tick(49); clock.fill(e,49,false,0);
    CHECK(e.utcMs == int64_t(rtc::kFirstUtc)*1000 + (uint64_t(1)<<32) && e.syncAgeMs == 5000 + (int64_t(1)<<32));
    clock.fill(e,49,true,999999);
    CHECK(e.timeSource == EventTimeSource::Gps && e.syncAgeMs == (int64_t(1)<<32)-4);
    clock.invalidateRtc(); clock.fill(e,49,false,0);
    CHECK(e.timeSource == EventTimeSource::Unknown && e.syncAgeMs == -1);
    std::printf("RTC pure helpers: %u checks passed\n", checks);
}
