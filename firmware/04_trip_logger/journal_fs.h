#pragma once
#include "event_log.h"
#include <cstdio>
#include <cstring>

namespace diagnostics {
// IO supplies checked VFS operations (including sync/close and directory EOF).
// The host substitutes the existing fault-injectable fake disks at this seam.
// All calls require FS_LOCK; allow() cancels maintenance between operations
// when a trip/upload requests the lock. No persistent handles survive a call.
template<class IO> class MountedJournalFs : public EventLogFs {
public:
    MountedJournalFs(IO& io, const bool& mounted, bool (*allow)())
        : io_(io), mounted_(mounted), allow_(allow) {}
    bool begin() override {
        return allowed() && io_.directory("/diag");
    }
    Result list(size_t index, Entry& entry) override {
        if (!allowed()) return Result::Error;
        return io_.list(index, entry, allow_);
    }
    bool read(const char* path, uint32_t offset, void* data, size_t size) override {
        return allowed() && pathOk(path) && io_.read(path, offset, data, size);
    }
    bool write(const char* path, const void* data, size_t size, bool append) override {
        return allowed() && pathOk(path) && size <= kFrameBytes &&
               io_.write(path, data, size, append);
    }
    bool rename(const char* from, const char* to) override {
        return allowed() && pathOk(from) && pathOk(to) && io_.rename(from, to);
    }
    bool remove(const char* path) override {
        return allowed() && pathOk(path) && io_.remove(path);
    }
    uint32_t allocatedBytes(uint32_t unit) {
        // Called by trip free-space checks with FS_LOCK, independently of the
        // maintenance gate. Count existing allocation, not reserved ack slots.
        if (!mounted_ || !unit) return 0;
        uint64_t bytes = 0;
        for (size_t i = 0; i <= 1024; ++i) {
            Entry entry{};
            Result result = io_.list(i, entry, nullptr, true);
            if (result == Result::End) return bytes > UINT32_MAX ? UINT32_MAX : bytes;
            if (result != Result::Ok) return 0; // conservatively keep full reserve
            bytes += ((static_cast<uint64_t>(entry.size) + unit - 1) / unit) * unit;
        }
        return 0;
    }
private:
    IO& io_;
    const bool& mounted_;
    bool (*allow_)();
    bool allowed() const { return mounted_ && (!allow_ || allow_()); }
    static bool pathOk(const char* path) {
        return path && std::strncmp(path, "/diag/", 6) == 0 && path[6] &&
               !std::strchr(path + 6, '/') && !std::strstr(path + 6, "..");
    }
};

// Reserve only the still-unallocated part: used filesystem bytes already
// include journal allocation. Subtracting the full 64 KiB again double counts.
inline uint64_t tripFreeWithJournal(uint64_t free, uint32_t journalAllocated) {
    uint32_t remaining = journalAllocated < kLittleFsJournalReserve ?
        kLittleFsJournalReserve - journalAllocated : 0;
    return free > remaining ? free - remaining : 0;
}
} // namespace diagnostics
