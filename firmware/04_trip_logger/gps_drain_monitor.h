// Core-1 UART drain cadence; independent of Arduino and GPS fix availability.
#pragma once
#include <stdint.h>

class GpsDrainMonitor {
public:
    static constexpr uint32_t STALL_MS = 1500;
    static constexpr uint32_t REPORT_MS = 60000;

    bool observe(uint32_t now) {
        if (!seen_) { seen_ = true; previous_ = now; return false; }
        lastGap_ = now - previous_; // unsigned elapsed time across millis wrap
        previous_ = now;
        if (lastGap_ > maximum_) maximum_ = lastGap_;
        if (lastGap_ <= STALL_MS) return false;
        if (count_ != UINT32_MAX) ++count_;
        return true;
    }
    // Call only for a new stall while debug is on. Quiet mode never consumes
    // the reporting allowance; rate limiting never suppresses observations.
    bool reportDue(uint32_t now) {
        if (reported_ && uint32_t(now - reportMs_) < REPORT_MS) return false;
        reported_ = true; reportMs_ = now; return true;
    }
    uint32_t count() const { return count_; }
    uint32_t maximumMs() const { return maximum_; }
    uint32_t lastGapMs() const { return lastGap_; }

private:
    bool seen_ = false, reported_ = false;
    uint32_t previous_ = 0, reportMs_ = 0;
    uint32_t count_ = 0, maximum_ = 0, lastGap_ = 0;
};
