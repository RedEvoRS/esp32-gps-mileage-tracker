// Runtime BOOT modifier. Sample only after reset/ROM strapping has finished.
#pragma once
#include <stdint.h>

namespace runtime_debug {
class BootButton {
public:
    static constexpr uint32_t kDebounceMs = 40;
    void sample(uint32_t now, bool low) {
        if (!sampled_ || low != low_) {
            sampled_ = true; low_ = low; changed_ = now;
        }
    }
    // A fresh high sample cancels the modifier immediately. A low must have
    // remained observed for the debounce interval, including across rollover.
    bool held(uint32_t now) const {
        return sampled_ && low_ && uint32_t(now - changed_) >= kDebounceMs;
    }
private:
    bool sampled_ = false, low_ = false;
    uint32_t changed_ = 0;
};
} // namespace runtime_debug
