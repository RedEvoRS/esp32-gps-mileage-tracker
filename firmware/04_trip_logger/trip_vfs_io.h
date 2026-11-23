#pragma once
#include <fcntl.h>
#include <unistd.h>

#if defined(JOURNAL_HOST_TEST)
#include <trip_vfs_fake_io.h>
#else
namespace trip_vfs_io {
using ::open;
using ::ftruncate;
using ::fsync;
using ::lseek;
using ::write;
using ::close;
}
#endif
