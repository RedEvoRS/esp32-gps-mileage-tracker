#pragma once
#include <cmath>

namespace charge_temperature {
// Plausibility bounds for the die sensor, not safe battery-charge limits.
static constexpr float kMinDieC = -40.0f;
static constexpr float kMaxDieC = 125.0f;
inline bool plausible(float dieC) {
    return std::isfinite(dieC) && dieC >= kMinDieC && dieC <= kMaxDieC;
}

struct Decision {
    bool pause;
    bool nextPauseRequested;
    bool unreadable;
    const char* why;
};

// Pure hysteresis decision for the charge temperature guard.
// Unreadable or implausible readings pause charging fail-safe without creating
// or clearing hot/cold hysteresis history.
inline Decision evaluate(float dieC, bool pauseRequested,
                         float hotPauseC, float hotResumeC,
                         float coldPauseC, float coldResumeC) {
    if (!plausible(dieC)) {
        return {true, pauseRequested, true, "temperature unreadable"};
    }
    if (pauseRequested) {
        const bool pause = !(dieC <= hotResumeC && dieC >= coldResumeC);
        return {pause, pause, false, dieC > hotResumeC ? "too hot" : "too cold"};
    }
    const bool pause = dieC >= hotPauseC || dieC <= coldPauseC;
    return {pause, pause, false, dieC >= hotPauseC ? "too hot" : "too cold"};
}
}
