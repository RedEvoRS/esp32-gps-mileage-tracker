#pragma once
#include <cmath>

namespace charge_temperature {
// Plausibility bounds for the die sensor, not safe battery-charge limits.
static constexpr float kMinDieC = -40.0f;
static constexpr float kMaxDieC = 125.0f;
inline bool plausible(float dieC) {
    return std::isfinite(dieC) && dieC >= kMinDieC && dieC <= kMaxDieC;
}
}
