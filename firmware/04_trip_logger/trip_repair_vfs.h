#pragma once
#include <cstdio>
#include <cstring>
#include "sd_write_check.h"
#include "trip_vfs_io.h"

// Use the already-mounted ESP-IDF VFS: Arduino File hides sync/close errors
// and has no truncate. The caller holds FS_LOCK and closes its read handle.
inline bool repairTripInPlace(const char* mount, const char* relative,
                              size_t cut, const char* suffix, const uint32_t* sdFailures = nullptr) {
    char path[96];
    int n = std::snprintf(path, sizeof(path), "%s%s", mount, relative);
    if (n <= 0 || static_cast<size_t>(n) >= sizeof(path)) return false;
    // No O_CREAT: a vanished trip must never become an epilogue-only file.
    const SdWriteCheck check(sdFailures);
    int fd = trip_vfs_io::open(path, O_WRONLY);
    if (fd < 0) return false;
    bool ok = check.ok() && trip_vfs_io::ftruncate(fd, static_cast<off_t>(cut)) == 0 && check.ok();
    // Commit the shortened FAT chain/size before an append can allocate again.
    // A reset here leaves every complete point and no closing GPX marker.
    if (ok) ok = trip_vfs_io::fsync(fd) == 0 && check.ok();
    // Set the offset explicitly: truncate need not move an open descriptor.
    if (ok) ok = trip_vfs_io::lseek(fd, static_cast<off_t>(cut), SEEK_SET) == static_cast<off_t>(cut);
    if (ok) ok = trip_vfs_io::write(fd, suffix, std::strlen(suffix)) ==
                 static_cast<ssize_t>(std::strlen(suffix));
    if (ok) ok = trip_vfs_io::fsync(fd) == 0;
    if (trip_vfs_io::close(fd) != 0) ok = false;
    return ok && check.ok();
}

// Rewriting an intact epilogue marks the FAT handle dirty for fsync while
// preserving the closing tags, even if only part of the rewrite succeeds.
inline bool rewriteTripEpilogue(const char* mount, const char* relative,
                                off_t offset, const char* epilogue, size_t size,
                                const uint32_t* sdFailures = nullptr) {
    char path[96];
    int n = std::snprintf(path, sizeof(path), "%s%s", mount, relative);
    if (n <= 0 || static_cast<size_t>(n) >= sizeof(path)) return false;
    const SdWriteCheck check(sdFailures);
    int fd = trip_vfs_io::open(path, O_WRONLY);
    if (fd < 0) return false;
    bool ok = check.ok() && trip_vfs_io::lseek(fd, offset, SEEK_SET) == offset;
    if (ok) ok = trip_vfs_io::write(fd, epilogue, size) == static_cast<ssize_t>(size);
    if (ok) ok = trip_vfs_io::fsync(fd) == 0;
    if (trip_vfs_io::close(fd) != 0) ok = false;
    return ok && check.ok();
}
