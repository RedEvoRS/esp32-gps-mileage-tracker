#pragma once
#include <cstdio>
#include <cstring>
#include "sd_write_check.h"
#include "trip_vfs_io.h"

// One descriptor per GPX batch, opened and closed under FS_LOCK. Direct VFS
// writes bypass stdio buffering, so write, sync and close failures are visible.
class FileWriter {
    int fd = -1;
    const SdWriteCheck sdWrite_;

public:
    size_t queued = 0;
    bool ok = false;

    FileWriter(const char* mount, const char* relative, bool create,
               const uint32_t* sdFailures = nullptr) : sdWrite_(sdFailures) {
        char path[96];
        int n = std::snprintf(path, sizeof(path), "%s%s", mount, relative);
        if (n <= 0 || static_cast<size_t>(n) >= sizeof(path)) return;
        // FAT treats O_APPEND as create-if-missing. Open the existing file
        // and seek to its end while the caller holds FS_LOCK.
        fd = create ? trip_vfs_io::openBatch(path, O_WRONLY | O_CREAT | O_EXCL, 0666)
                    : trip_vfs_io::openBatch(path, O_WRONLY);
        // A trip must already have bytes on disk before appending points.
        ok = fd >= 0 && (create || trip_vfs_io::lseek(fd, 0, SEEK_END) > 0) && sdWrite_.ok();
    }
    FileWriter(const FileWriter&) = delete;
    FileWriter& operator=(const FileWriter&) = delete;
    ~FileWriter() { if (fd >= 0) trip_vfs_io::close(fd); }

    explicit operator bool() const { return fd >= 0; }

    void put(const char* s, size_t n) {
        if (!ok) return;
        if (trip_vfs_io::write(fd, s, n) == static_cast<ssize_t>(n) && sdWrite_.ok()) queued += n;
        else ok = false;
    }
    void put(const char* s) { put(s, std::strlen(s)); }

    bool commit() {
        if (fd < 0) return false;
        if (ok) ok = trip_vfs_io::fsync(fd) == 0;
        if (trip_vfs_io::close(fd) != 0) ok = false;
        fd = -1;
        return ok && sdWrite_.ok();
    }
};
