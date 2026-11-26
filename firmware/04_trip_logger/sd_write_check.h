#pragma once
#include <cstdint>

// The SD disk callback and its callers share FS_LOCK (or run before the
// workers start). Leave the counter alone across unmount and remount.
inline uint32_t& sdWriteFailureCount() {
    static uint32_t count = 0;
    return count;
}

class SdWriteCheck {
    const uint32_t* count_;
    const uint32_t before_;

public:
    explicit SdWriteCheck(const uint32_t* count)
        : count_(count), before_(count ? *count : 0) {}

    bool ok() const { return !count_ || *count_ == before_; }
};
