// Exercise the production mounted VFS implementation, including error returns
// Arduino File hides. Windows shims only adapt POSIX spelling/binary mode.
#include "event_log.h"
#include <cassert>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <filesystem>
#include <fstream>
#ifdef _WIN32
#include <io.h>
#include <direct.h>
#endif
static bool failSync = false, failClose = false, failList = false, failOpen = false, failTruncate = false;
static int checkedTruncate(int fd, off_t size) {
    if (failTruncate) { errno = EIO; return -1; }
#ifdef _WIN32
    return ::_chsize_s(fd, size) == 0 ? 0 : -1;
#else
    return ::ftruncate(fd, size);
#endif
}
static int checkedSync(int fd) {
    if (failSync) { errno = EIO; return -1; }
#ifdef _WIN32
    return ::_commit(fd);
#else
    return ::fsync(fd);
#endif
}
static int checkedClose(int fd) {
    int result = ::close(fd);
    if (failClose) { errno = EIO; return -1; }
    return result;
}
static dirent* checkedReadDir(DIR* dir) {
    if (failList) { errno = EIO; return nullptr; }
    return ::readdir(dir);
}
static int checkedMkdir(const char* path, [[maybe_unused]] int mode) {
#ifdef _WIN32
    return ::_mkdir(path);
#else
    return ::mkdir(path, mode);
#endif
}
static int checkedOpen(const char* path, int flags, int mode = 0) {
    if (failOpen) { errno = EIO; return -1; }
#ifdef _WIN32
    return ::open(path, flags | _O_BINARY, mode);
#else
    return ::open(path, flags, mode);
#endif
}
#define fsync checkedSync
#define close checkedClose
#define readdir checkedReadDir
#define mkdir checkedMkdir
#define open checkedOpen
#include "journal_vfs.h"
// Exercise the new repair helper on real host files as well as fake disks.
#undef JOURNAL_HOST_TEST
#define ftruncate checkedTruncate
#include "trip_repair_vfs.h"
#undef ftruncate
#undef fsync
#undef close
#undef readdir
#undef mkdir
#undef open
#include "journal_fs.h"

int main(int argc, char** argv) {
    assert(argc == 2);
    std::filesystem::path root = std::filesystem::absolute(argv[1]);
    std::filesystem::create_directories(root);
    std::string mount = root.generic_string();
    diagnostics::JournalVfs io(mount.c_str());
    bool mounted = true;
    diagnostics::MountedJournalFs<diagnostics::JournalVfs> adapter(io, mounted, nullptr);
    using Result = diagnostics::EventLogFs::Result;
    using Operation = diagnostics::EventLogFs::Operation;
    assert(adapter.begin());
    diagnostics::EventLogFs::Entry entry;
    assert(adapter.list(0, entry) == Result::End);
    const char bytes[] = "one\ntwo\n";
    assert(adapter.write("/diag/seg-00000001.open", bytes, sizeof(bytes)-1, false));
    assert(adapter.write("/diag/seg-00000001.open", "tail", 4, true));
    char buffer[16] = {};
    assert(adapter.read("/diag/seg-00000001.open", 4, buffer, 4));
    assert(!std::memcmp(buffer, "two\n", 4));
    assert(!adapter.read("/diag/seg-00000001.open", 0, buffer, 16));
    assert(adapter.list(0, entry) == Result::Ok && entry.size == sizeof(bytes)-1+4);
    assert(!std::strcmp(entry.name, "seg-00000001.open"));
    assert(adapter.rename("/diag/seg-00000001.open", "/diag/seg-00000001.ready"));
    assert(adapter.write("/diag/seg-00000002.open", "keep", 4, false));
    assert(!adapter.rename("/diag/seg-00000002.open", "/diag/seg-00000001.ready"));
    failOpen = true;
    assert(!adapter.write("/diag/ack-0", "test", 4, false));
    assert(adapter.writeFailure() == Operation::Open);
    failOpen = false; failSync = true;
    assert(!adapter.write("/diag/ack-0", "test", 4, false));
    assert(adapter.writeFailure() == Operation::Write);
    failClose = true;
    assert(!adapter.write("/diag/ack-0", "test", 4, false));
    assert(adapter.writeFailure() == Operation::Write); // preserve the first error
    failSync = false; failClose = true;
    assert(!adapter.write("/diag/ack-0", "test", 4, false));
    assert(adapter.writeFailure() == Operation::Close);
    assert(!adapter.read("/diag/ack-0", 0, buffer, 4));
    failClose = false; failList = true;
    assert(adapter.list(0, entry) == Result::Error);
    failList = false;
    assert(!adapter.write("/trips/unuploaded.gpx", "bad", 3, false));
    assert(!adapter.remove("/diag/../trip"));
    assert(!adapter.rename("/diag/ack-0", "/trips/trip"));
    mounted = false; assert(!adapter.begin()); mounted = true;
    for (const char* name : {"seg-00000001.ready", "seg-00000002.open", "ack-0"}) {
        std::string path = std::string("/diag/") + name; assert(adapter.remove(path.c_str()));
    }
    assert(adapter.list(0, entry) == Result::End);
    std::filesystem::create_directory(root / "diag" / "unexpected");
    assert(adapter.list(0, entry) == Result::Error);
    std::filesystem::remove(root / "diag" / "unexpected");
    std::filesystem::remove(root / "diag");
    const std::string prefix = "retained point</trkpt>";
    const std::string original = prefix + "torn tail";
    const char* suffix = "\n</trkseg></trk></gpx>\n";
    auto trip = root / "trip.gpx";
    auto readTrip = [&]() {
        std::ifstream input(trip, std::ios::binary);
        return std::string(std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>());
    };
    for (int fault = 0; fault < 5; ++fault) {
        { std::ofstream output(trip, std::ios::binary); output << original; }
        failOpen = fault == 0; failTruncate = fault == 1;
        failSync = fault == 2; failClose = fault == 3;
        assert(repairTripInPlace(mount.c_str(), "/trip.gpx", prefix.size(), suffix) == (fault == 4));
        assert(readTrip().starts_with(prefix));
        if (fault < 2) assert(readTrip() == original);
        if (fault == 2) assert(readTrip() == prefix);
        failOpen = failTruncate = failSync = failClose = false;
        assert(repairTripInPlace(mount.c_str(), "/trip.gpx", prefix.size(), suffix));
        assert(readTrip() == prefix + suffix);
    }
    assert(!repairTripInPlace(mount.c_str(), "/missing.gpx", 8, suffix));
    assert(!std::filesystem::exists(root / "missing.gpx"));
    std::filesystem::remove(trip);
    std::filesystem::remove(root);
    std::puts("Journal production VFS round trip, sync/close/list errors and path isolation passed");
    std::puts("Trip production VFS truncate/append, open/truncate/sync/close errors and missing-file safety passed");
}
