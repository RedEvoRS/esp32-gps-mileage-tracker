#pragma once
#include <cmath>

namespace charge_temperature {
// Plausibility bounds for the die sensor, not safe battery-charge limits.
static constexpr float kMinDieC = -40.0f;
static constexpr float kMaxDieC = 125.0f;
inline bool plausible(float dieC) {
    return std::isfinite(dieC) && dieC >= kMinDieC && dieC <= kMaxDieC;
}

inline bool plausibleAir(float airC) {
    return airC >= -40.0f && airC <= 85.0f;
}

struct Limits {
    bool hot;
    bool cold;
};

struct Decision {
    bool pause;
    bool nextPauseRequested;
    bool unreadable;
    Limits die;
    Limits air;
    bool hot() const { return die.hot || air.hot; }
    bool cold() const { return die.cold || air.cold; }
};

// Thresholds are in battery terms. Pass NAN when this check has no fresh air
// sample. An invalid die blocks charging and keeps any existing temperature
// pause. A plausible air reading can also request a pause while the die is invalid.
inline Decision evaluate(float dieC, float airC, bool pauseRequested,
                         float hotPauseC, float hotResumeC,
                         float coldPauseC, float coldResumeC, float dieOffsetC) {
    const auto limits = [&](float c) -> Limits {
        if (pauseRequested) return {c > hotResumeC, c < coldResumeC};
        return {c >= hotPauseC, c <= coldPauseC};
    };
    const Limits air = plausibleAir(airC) ? limits(airC) : Limits{};
    if (!plausible(dieC)) {
        return {true, pauseRequested || air.hot || air.cold, true, {}, air};
    }
    const Limits die = limits(dieC - dieOffsetC);
    const bool pause = die.hot || die.cold || air.hot || air.cold;
    return {pause, pause, false, die, air};
}
}
