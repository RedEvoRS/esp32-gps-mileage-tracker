#pragma once
#include "arduino_stub.h"
#include "event_log.h"

// Checked IO seam over the same fake SD/LittleFS disks as trip tests. Actual
// POSIX sync/close/EOF handling is separately exercised by test_journal_vfs.
class JournalPlatformIo {
    fs::FS& fs_;
public:
    explicit JournalPlatformIo(const char* mount) : fs_(std::strcmp(mount, "/sd") == 0 ?
        static_cast<fs::FS&>(SD) : static_cast<fs::FS&>(LittleFS)) {}
    bool directory(const char* path) {
        assertLocked();
        if (!fs_.g_disk.mounted || fs_.g_disk.failMount) return false;
        if (fs_.exists(path)) { auto file = fs_.open(path); return file && file.isDirectory(); }
        return fs_.mkdir(path);
    }
    diagnostics::EventLogFs::Result list(size_t index, diagnostics::EventLogFs::Entry& entry,
                                       bool (*allow)() = nullptr, bool accounting = false) {
        if (!accounting) assertLocked();
        using Result = diagnostics::EventLogFs::Result;
        if (!fs_.g_disk.mounted || fs_.g_disk.failMount || !fs_.exists("/diag")) return Result::Error;
        size_t i = 0;
        for (const auto& file : fs_.g_disk.files) {
            if (allow && !allow()) return Result::Error;
            if (file.first.rfind("/diag/", 0)) continue;
            std::string name = file.first.substr(6);
            if (name.size() >= sizeof(entry.name) || name.find('/') != std::string::npos) return Result::Error;
            if (i++ == index) {
                std::strcpy(entry.name, name.c_str()); entry.size = file.second.size(); return Result::Ok;
            }
        }
        return Result::End;
    }
    bool read(const char* path, uint32_t offset, void* data, size_t bytes) {
        assertLocked();
        if (fs_.g_disk.readsFail) return false;
        auto file = fs_.open(path);
        return file && file.seek(offset) && file.read(static_cast<uint8_t*>(data), bytes) == bytes;
    }
    bool write(const char* path, const void* data, size_t bytes, bool append) {
        assertLocked();
        if (fs_.g_disk.writesFail) return false;
        size_t old = append && fs_.g_disk.files.count(path) ? fs_.g_disk.files[path].size() : 0;
        auto file = fs_.open(path, append ? FILE_APPEND : FILE_WRITE);
        if (!file) return false;
        bool ok = file.write(static_cast<const uint8_t*>(data), bytes) == bytes;
        file.close();
        return ok && fs_.g_disk.files[path].size() == old + bytes;
    }
    bool rename(const char* from, const char* to) {
        assertLocked(); return !fs_.exists(to) && fs_.rename(from, to);
    }
    bool remove(const char* path) { assertLocked(); return fs_.remove(path); }
private:
    void assertLocked() {
        if (g_lockDepth != 1) { std::fprintf(stderr, "journal IO outside FS_LOCK\n"); std::abort(); }
    }
};
