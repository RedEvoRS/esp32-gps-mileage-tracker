// Startup policy only: no hardware, storage, clocks or side effects.
#pragma once
#include <stdint.h>

namespace startup {
enum class Mode { Normal, PmuMissing, PmuUntrusted, NoStorage };
inline Mode selectMode(bool pmuReady, bool radioOff, bool storageReady) {
    return !pmuReady ? Mode::PmuMissing : !radioOff ? Mode::PmuUntrusted :
           !storageReady ? Mode::NoStorage : Mode::Normal;
}
class DegradedPolicy {
public:
    static constexpr uint32_t kFirstMs = 60000, kCapMs = 600000;
    void begin(Mode mode, uint32_t now) { mode_ = mode; since_ = now; wait_ = kFirstMs; }
    Mode mode() const { return mode_; }
    uint32_t waitMs() const { return wait_; }
    bool retryDue(uint32_t now) const {
        return mode_ != Mode::Normal && uint32_t(now - since_) >= wait_;
    }
    // Call after the bounded retry finishes; time spent retrying cannot shorten
    // the next interval. Success permits only a fresh normal boot, never resume.
    bool retryResult(uint32_t now, bool success) {
        if (!retryDue(now)) return false;
        since_ = now;
        if (wait_ < kCapMs) wait_ = wait_ > kCapMs / 2 ? kCapMs : wait_ * 2;
        return success;
    }
private:
    Mode mode_ = Mode::Normal;
    uint32_t since_ = 0, wait_ = kFirstMs;
};
} // namespace startup
