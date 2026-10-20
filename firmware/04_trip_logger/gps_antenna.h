// Core-1-only antenna evidence; no I/O, fix-state or trip-state dependency.
#pragma once
#include "debug_gsv.h"

class GpsAntenna {
public:
    // First guesses from one Supreme/MAX-M10S and one active antenna,
    // bench 2026-10-19: unplugged AGC 79%, strong=0, max C/N0 15-20;
    // outdoors 29%/22 strong; indoors 6-18%/0-3 strong. The initial
    // unplugged 29% reading must not count toward the suspect hold.
    static constexpr unsigned kSuspectAgcPct = 70, kHealthyAgcPct = 50;
    static constexpr uint32_t kHoldMs = 120000;

    void update(uint32_t now, bool rfValid, unsigned agcPct, const DebugSignal& signal) {
        bool fresh = signal.heard >= 0;
        bool healthy = (rfValid && agcPct <= kHealthyAgcPct) ||
                       (fresh && signal.strong >= 1);
        bool suspect = rfValid && agcPct >= kSuspectAgcPct && fresh && signal.strong == 0;
        if (healthy) {
            observed_ = true; active_ = false; holding_ = false;
        } else if (suspect) {
            if (!holding_) { holding_ = true; suspectSince_ = now; }
            if (uint32_t(now - suspectSince_) >= kHoldMs) observed_ = active_ = true;
        } else {
            // No evidence breaks the hold but retains the last verdict,
            // including unknown at boot and an already active warning.
            holding_ = false;
        }
    }
    bool observed() const { return observed_; }
    bool active() const { return active_; }

private:
    bool observed_ = false, active_ = false, holding_ = false;
    uint32_t suspectSince_ = 0;
};
