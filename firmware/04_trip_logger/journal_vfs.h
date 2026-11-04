#pragma once
#include "event_log.h"
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

namespace diagnostics {
// Arduino File::close()/flush() discard errors. Use the same mounted ESP-IDF
// VFS directly so fsync, close and readdir failures reach EventLog. Never mount
// or format here. Paths supplied to EventLog remain relative to the store.
class JournalVfs {
public:
    explicit JournalVfs(const char* mount) : mount_(mount) {}
    bool directory(const char* relative) {
        char path[96]; if (!full(relative, path)) return false;
        struct stat info;
        if (::stat(path, &info) == 0) return S_ISDIR(info.st_mode);
        return errno == ENOENT && ::mkdir(path, 0700) == 0;
    }
    EventLogFs::Result list(size_t index, EventLogFs::Entry& entry,
                           bool (*allow)() = nullptr, bool = false) {
        using Result = EventLogFs::Result;
        char path[96]; if (!full("/diag", path)) return Result::Error;
        DIR* dir = ::opendir(path); if (!dir) return Result::Error;
        Result result = Result::End;
        size_t n = 0;
        for (;;) {
            if (allow && !allow()) { result = Result::Error; break; }
            errno = 0;
            dirent* item = ::readdir(dir);
            if (!item) { if (errno) result = Result::Error; break; }
            if (!std::strcmp(item->d_name, ".") || !std::strcmp(item->d_name, "..")) continue;
            // Subdirectories/oversize entries fail closed; do not silently omit
            // unknown allocation from the quota or truncate a filename.
            char relative[64];
            int len = std::snprintf(relative, sizeof(relative), "/diag/%s", item->d_name);
            struct stat info;
            if (len < 0 || static_cast<size_t>(len) >= sizeof(relative) ||
                !full(relative, path) || ::stat(path, &info) || !S_ISREG(info.st_mode) ||
                info.st_size < 0 || static_cast<uint64_t>(info.st_size) > UINT32_MAX ||
                std::strlen(item->d_name) >= sizeof(entry.name)) { result = Result::Error; break; }
            if (n++ == index) {
                std::strcpy(entry.name, item->d_name); entry.size = info.st_size;
                result = Result::Ok; break;
            }
        }
        if (::closedir(dir)) result = Result::Error;
        return result;
    }
    bool read(const char* relative, uint32_t offset, void* data, size_t size) {
        char path[96]; if (!full(relative, path)) return false;
        int fd = ::open(path, O_RDONLY); if (fd < 0) return false;
        bool ok = ::lseek(fd, offset, SEEK_SET) == static_cast<off_t>(offset);
        if (ok) ok = ::read(fd, data, size) == static_cast<ssize_t>(size);
        if (::close(fd)) ok = false;
        return ok;
    }
    bool write(const char* relative, const void* data, size_t size, bool append) {
        writeFailure_ = EventLogFs::Operation::Open;
        char path[96]; if (!full(relative, path)) return false;
        int fd = ::open(path, O_WRONLY | O_CREAT | (append ? O_APPEND : O_TRUNC), 0600);
        if (fd < 0) return false;
        writeFailure_ = EventLogFs::Operation::Write;
        bool ok = ::write(fd, data, size) == static_cast<ssize_t>(size);
        if (::fsync(fd)) ok = false;
        if (::close(fd)) {
            if (ok) writeFailure_ = EventLogFs::Operation::Close;
            ok = false;
        }
        return ok;
    }
    EventLogFs::Operation writeFailure() const { return writeFailure_; }
    bool rename(const char* from, const char* to) {
        char a[96], b[96]; if (!full(from, a) || !full(to, b)) return false;
        struct stat info;
        if (::stat(b, &info) == 0 || errno != ENOENT) return false;
        // EventLog exclusively owns /diag and FS_LOCK serializes both stores.
        return ::rename(a, b) == 0;
    }
    bool remove(const char* relative) {
        char path[96]; return full(relative, path) && ::unlink(path) == 0;
    }
private:
    const char* mount_;
    EventLogFs::Operation writeFailure_ = EventLogFs::Operation::Write;
    bool full(const char* relative, char (&path)[96]) {
        int n = std::snprintf(path, sizeof(path), "%s%s", mount_, relative);
        return n > 0 && static_cast<size_t>(n) < sizeof(path);
    }
};
} // namespace diagnostics
