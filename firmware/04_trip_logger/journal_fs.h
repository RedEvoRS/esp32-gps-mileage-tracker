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
        return checkAllowed() && io_.directory("/diag");
    }
    Result list(size_t skip, ListVisitor visitor, void* context) override {
        deferred_ = false;
        if (!mounted_) return Result::Error;
        if (allow_ && !allow_()) { deferred_ = true; return Result::Deferred; }
        Result result = io_.list(skip, visitor, context, allow_);
        if (result == Result::Deferred) deferred_ = true;
        return result;
    }
    bool read(const char* path, uint32_t offset, void* data, size_t size) override {
        return checkAllowed() && pathOk(path) && io_.read(path, offset, data, size);
    }
    bool write(const char* path, const void* data, size_t size, bool append) override {
        return checkAllowed() && pathOk(path) && size <= kFrameBytes &&
               io_.write(path, data, size, append);
    }
    bool rename(const char* from, const char* to) override {
        return checkAllowed() && pathOk(from) && pathOk(to) && io_.rename(from, to);
    }
    bool remove(const char* path) override {
        return checkAllowed() && pathOk(path) && io_.remove(path);
    }
    Operation writeFailure() const override { return io_.writeFailure(); }
    bool deferred() const override { return deferred_ || (allow_ && !allow_()); }
    uint32_t allocatedBytes(uint32_t unit) {
        // Called by trip free-space checks with FS_LOCK, independently of the
        // maintenance gate. Count existing allocation, not reserved ack slots.
        if (!mounted_ || !unit) return 0;
        struct State { uint32_t unit; uint64_t bytes; size_t count; };
        State state{unit, 0, 0};
        Result result = io_.list(0, [](void* context, const Entry& entry) {
            auto& s = *static_cast<State*>(context);
            if (s.count >= 1024) return false;
            ++s.count;
            s.bytes += ((static_cast<uint64_t>(entry.size) + s.unit - 1) / s.unit) * s.unit;
            return true;
        }, &state, nullptr, true);
        if (result != Result::Ok) return 0; // conservatively keep full reserve
        return state.bytes > UINT32_MAX ? UINT32_MAX : static_cast<uint32_t>(state.bytes);
    }
private:
    IO& io_;
    const bool& mounted_;
    bool (*allow_)();
    bool deferred_ = false;
    bool checkAllowed() {
        deferred_ = false;
        if (!mounted_) return false;
        if (allow_ && !allow_()) { deferred_ = true; return false; }
        return true;
    }
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
