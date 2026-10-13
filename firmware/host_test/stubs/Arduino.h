// Arduino.h for building the real TinyGPSPlus on a PC (test_gps).
#pragma once
#include "arduino_stub.h"
typedef uint8_t byte;
#define TWO_PI (2.0 * M_PI)
inline double degrees(double r) { return r * 180.0 / M_PI; }
template <typename T> inline T sq(T x) { return x * x; }
