#pragma once
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <unistd.h>

#if defined(JOURNAL_HOST_TEST)
#include <trip_repair_fake_io.h>
#else
namespace trip_repair_io {
using ::open;
using ::ftruncate;
using ::fsync;
using ::lseek;
using ::write;
using ::close;
}
#endif

// Use the already-mounted ESP-IDF VFS: Arduino File hides sync/close errors
// and has no truncate. The caller holds FS_LOCK and closes its read handle.
inline bool repairTripInPlace(const char* mount, const char* relative,
                              size_t cut, const char* suffix) {
    char path[96];
    int n = std::snprintf(path, sizeof(path), "%s%s", mount, relative);
    if (n <= 0 || static_cast<size_t>(n) >= sizeof(path)) return false;
    // No O_CREAT: a vanished trip must never become an epilogue-only file.
    int fd = trip_repair_io::open(path, O_WRONLY);
    if (fd < 0) return false;
    bool ok = trip_repair_io::ftruncate(fd, static_cast<off_t>(cut)) == 0;
    // Commit the shortened FAT chain/size before an append can allocate again.
    // A reset here leaves every complete point and no closing GPX marker.
    if (ok) ok = trip_repair_io::fsync(fd) == 0;
    // Set the offset explicitly: truncate need not move an open descriptor.
    if (ok) ok = trip_repair_io::lseek(fd, static_cast<off_t>(cut), SEEK_SET) == static_cast<off_t>(cut);
    if (ok) ok = trip_repair_io::write(fd, suffix, std::strlen(suffix)) ==
                 static_cast<ssize_t>(std::strlen(suffix));
    if (ok) ok = trip_repair_io::fsync(fd) == 0;
    if (trip_repair_io::close(fd) != 0) ok = false;
    return ok;
}
