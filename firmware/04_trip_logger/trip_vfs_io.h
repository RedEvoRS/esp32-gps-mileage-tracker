#pragma once
#include <fcntl.h>
#include <unistd.h>

#if defined(JOURNAL_HOST_TEST)
#include <trip_vfs_fake_io.h>
#else
namespace trip_vfs_io {
using ::open;
inline int openBatch(const char* path, int flags, int mode = 0) {
    return ::open(path, flags, mode);
}
using ::ftruncate;
using ::fsync;
using ::lseek;
using ::write;
using ::close;
}
#endif
