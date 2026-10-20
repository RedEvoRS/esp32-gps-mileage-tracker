// Compile-only stand-in for TinyGPSPlus. test_fs drives the trip FSM with
// synthetic fixes, so nothing here reports a fix. test_gps builds against
// the real library instead (see run.sh).
#pragma once
#include "arduino_stub.h"

struct TinyGPSLocation { uint32_t age() const { return UINT32_MAX; } bool isValid() { return false; } bool isUpdated() { return false; } double lat() { return 0; } double lng() { return 0; } };
struct TinyGPSDate { bool isUpdated() { return false; } uint32_t age() { return 0; } bool isValid() { return false; } int year() { return 0; } int month() { return 1; } int day() { return 1; } };
struct TinyGPSTime { bool isUpdated() { return false; } int centisecond() { return 0; } bool isValid() { return false; } int hour() { return 0; } int minute() { return 0; } int second() { return 0; } uint32_t age() { return 0; } };
struct TinyGPSAltitude { bool isValid() { return false; } double meters() { return 0; } };
struct TinyGPSSpeed { uint32_t age() const { return UINT32_MAX; } bool isValid() { return false; } double mps() { return 0; } double mph() { return 0; } };
struct TinyGPSCourse { bool isValid() { return false; } double deg() { return 0; } };
struct TinyGPSHDOP { uint32_t age() const { return UINT32_MAX; } bool isValid() { return false; } double hdop() { return 0; } };
struct TinyGPSInteger { uint32_t age() const { return UINT32_MAX; } bool isValid() { return false; } uint32_t value() { return 0; } };
struct TinyGPSPlus {
    TinyGPSLocation location; TinyGPSDate date; TinyGPSTime time; TinyGPSAltitude altitude;
    TinyGPSSpeed speed; TinyGPSCourse course; TinyGPSHDOP hdop; TinyGPSInteger satellites;
    bool encode(int) { return false; }
    uint32_t passedChecksum() const { return 0; }
};
struct TinyGPSCustom {
    bool isValid() const { return false; }
    uint32_t age() const { return UINT32_MAX; }
    TinyGPSCustom(TinyGPSPlus&, const char*, int) {}
    bool isUpdated() const { return false; }
    const char* value() { return ""; }
};
