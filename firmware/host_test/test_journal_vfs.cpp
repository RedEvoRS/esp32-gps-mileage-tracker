// Exercise the production mounted VFS implementation, including error returns
// Arduino File hides. Windows shims only adapt POSIX spelling/binary mode.
#include "event_log.h"
#include "sd_write_check.h"
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
#include <vector>
#include <string>
static bool failSync = false, failClose = false, failList = false, failOpen = false, failTruncate = false;
enum class HiddenWriteError { None, Open, Sync, Close, Truncate };
static HiddenWriteError hiddenWriteError = HiddenWriteError::None;
static void simulateDiskWrite(HiddenWriteError point) {
    // Model a failed sector write whose error FatFs discards. The POSIX
    // syscall below still returns its real result, normally success.
    if (hiddenWriteError == point) ++sdWriteFailureCount();
}
static size_t openDirCount = 0, closeDirCount = 0, statCount = 0;
static DIR* checkedOpenDir(const char* path) {
    DIR* d = ::opendir(path);
    if (d) ++openDirCount;
    return d;
}
static int checkedCloseDir(DIR* dir) {
    ++closeDirCount;
    return ::closedir(dir);
}
static int checkedStat(const char* path, struct stat* info) {
    ++statCount;
    return ::stat(path, info);
}
static int checkedTruncate(int fd, off_t size) {
    simulateDiskWrite(HiddenWriteError::Truncate);
    if (failTruncate) { errno = EIO; return -1; }
#ifdef _WIN32
    return ::_chsize_s(fd, size) == 0 ? 0 : -1;
#else
    return ::ftruncate(fd, size);
#endif
}
static int checkedSync(int fd) {
    simulateDiskWrite(HiddenWriteError::Sync);
    if (failSync) { errno = EIO; return -1; }
#ifdef _WIN32
    return ::_commit(fd);
#else
    return ::fsync(fd);
#endif
}
static int checkedClose(int fd) {
    simulateDiskWrite(HiddenWriteError::Close);
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
    simulateDiskWrite(HiddenWriteError::Open);
    if (failOpen) { errno = EIO; return -1; }
#ifdef _WIN32
    return ::open(path, flags | _O_BINARY, mode);
#else
    return ::open(path, flags, mode);
#endif
}
#define fsync checkedSync
#define close checkedClose
#define opendir checkedOpenDir
#define closedir checkedCloseDir
#define readdir checkedReadDir
#define stat(path, info) checkedStat(path, info)
#define mkdir checkedMkdir
#define open checkedOpen
#include "journal_vfs.h"
// Exercise the new repair helper on real host files as well as fake disks.
#undef JOURNAL_HOST_TEST
#define ftruncate checkedTruncate
#include "trip_repair_vfs.h"
#include "trip_write_vfs.h"
#undef ftruncate
#undef fsync
#undef close
#undef opendir
#undef closedir
#undef readdir
#undef stat
#undef mkdir
#undef open
#include "journal_fs.h"

static bool gAllowList = true;
static size_t gAllowLimit = SIZE_MAX, gAllowVisited = 0;
static bool allowList() { return gAllowList && gAllowVisited < gAllowLimit; }

int main(int argc, char** argv) {
    assert(argc == 2);
    std::filesystem::path root = std::filesystem::absolute(argv[1]);
    std::filesystem::create_directories(root);
    std::string mount = root.generic_string();
    diagnostics::JournalVfs io(mount.c_str());
    bool mounted = true;
    diagnostics::MountedJournalFs<diagnostics::JournalVfs> adapter(io, mounted, allowList);
    using Result = diagnostics::EventLogFs::Result;
    using Operation = diagnostics::EventLogFs::Operation;
    auto listAll = [&](size_t skip, std::vector<diagnostics::EventLogFs::Entry>& out) {
        return adapter.list(skip, [](void* ctx, const diagnostics::EventLogFs::Entry& entry) {
            ++gAllowVisited;
            static_cast<std::vector<diagnostics::EventLogFs::Entry>*>(ctx)->push_back(entry);
            return true;
        }, &out);
    };
    assert(adapter.begin());
    std::vector<diagnostics::EventLogFs::Entry> entries;
    assert(listAll(0, entries) == Result::Ok && entries.empty());
    const char bytes[] = "one\ntwo\n";
    assert(adapter.write("/diag/seg-00000001.open", bytes, sizeof(bytes)-1, false));
    assert(adapter.write("/diag/seg-00000001.open", "tail", 4, true));
    char buffer[16] = {};
    assert(adapter.read("/diag/seg-00000001.open", 4, buffer, 4));
    assert(!std::memcmp(buffer, "two\n", 4));
    assert(!adapter.read("/diag/seg-00000001.open", 0, buffer, 16));
    entries.clear();
    assert(listAll(0, entries) == Result::Ok && entries.size() == 1);
    assert(entries[0].size == sizeof(bytes)-1+4 && !std::strcmp(entries[0].name, "seg-00000001.open"));
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
    entries.clear();
    assert(listAll(0, entries) == Result::Error);
    failList = false;
    assert(!adapter.write("/trips/unuploaded.gpx", "bad", 3, false));
    assert(!adapter.remove("/diag/../trip"));
    assert(!adapter.rename("/diag/ack-0", "/trips/trip"));
    mounted = false; assert(!adapter.begin()); mounted = true;
    for (const char* name : {"seg-00000001.ready", "seg-00000002.open", "ack-0"}) {
        std::string path = std::string("/diag/") + name; assert(adapter.remove(path.c_str()));
    }
    entries.clear();
    assert(listAll(0, entries) == Result::Ok && entries.empty());
    // Issue #797: single-pass O(N) enumeration (1 opendir, 1 stat per entry),
    // clean handle close on cooperative deferral, and zero re-stats on resume.
    for (unsigned i = 1; i <= 100; ++i) {
        char path[40];
        std::snprintf(path, sizeof(path), "/diag/seg-%08u.ready", i);
        assert(adapter.write(path, "data", 4, false));
    }
    openDirCount = closeDirCount = statCount = gAllowVisited = 0; gAllowLimit = SIZE_MAX;
    entries.clear();
    assert(listAll(0, entries) == Result::Ok && entries.size() == 100);
    assert(openDirCount == 1 && closeDirCount == 1 && statCount == 100);
    // Interrupt after 30 entries, then resume from skip=30 without re-statting.
    openDirCount = closeDirCount = statCount = gAllowVisited = 0; gAllowLimit = 30;
    entries.clear();
    assert(listAll(0, entries) == Result::Deferred && adapter.deferred());
    assert(entries.size() == 30 && openDirCount == 1 && closeDirCount == 1 && statCount == 30);
    gAllowLimit = SIZE_MAX;
    assert(listAll(entries.size(), entries) == Result::Ok && entries.size() == 100);
    assert(openDirCount == 2 && closeDirCount == 2 && statCount == 100);
    for (unsigned i = 1; i <= 100; ++i) {
        char path[40];
        std::snprintf(path, sizeof(path), "/diag/seg-%08u.ready", i);
        assert(adapter.remove(path));
    }
    std::filesystem::create_directory(root / "diag" / "unexpected");
    entries.clear();
    assert(listAll(0, entries) == Result::Error);
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
    diagnostics::JournalVfs sdIo(mount.c_str(), &sdWriteFailureCount());
    assert(sdIo.directory("/diag"));
    for (auto point : {HiddenWriteError::Open, HiddenWriteError::Sync, HiddenWriteError::Close}) {
        hiddenWriteError = point;
        assert(!sdIo.write("/diag/ack-0", "test", 4, false));
        const auto expected = point == HiddenWriteError::Open ? Operation::Open :
                              point == HiddenWriteError::Close ? Operation::Close : Operation::Write;
        assert(sdIo.writeFailure() == expected);
        assert(io.write("/diag/ack-0", "test", 4, false)); // unmonitored store
        {
            FileWriter writer(mount.c_str(), "/trip.gpx", false, &sdWriteFailureCount());
            assert(writer); // open returned a valid descriptor
            writer.put(suffix);
            assert(!writer.commit());
        }
        hiddenWriteError = HiddenWriteError::None;
        assert(sdIo.write("/diag/ack-0", "test", 4, false));
    }
    {
        FileWriter writer(mount.c_str(), "/trip.gpx", false, &sdWriteFailureCount());
        writer.put(suffix);
        assert(writer.commit());
    }
    { std::ofstream output(trip, std::ios::binary); output << original; }
    hiddenWriteError = HiddenWriteError::Truncate;
    assert(!repairTripInPlace(mount.c_str(), "/trip.gpx", prefix.size(), suffix, &sdWriteFailureCount()));
    assert(readTrip() == prefix); // stop before the append when truncate reports success
    hiddenWriteError = HiddenWriteError::None;
    assert(repairTripInPlace(mount.c_str(), "/trip.gpx", prefix.size(), suffix, &sdWriteFailureCount()));
    hiddenWriteError = HiddenWriteError::Close;
    assert(!rewriteTripEpilogue(mount.c_str(), "/trip.gpx", prefix.size(),
                                suffix, std::strlen(suffix), &sdWriteFailureCount()));
    hiddenWriteError = HiddenWriteError::None;
    assert(sdIo.remove("/diag/ack-0"));
    std::filesystem::remove(root / "diag");
    std::filesystem::remove(trip);
    std::filesystem::remove(root);
    std::puts("Journal production VFS round trip, sync/close/list errors and path isolation passed");
    std::puts("Trip production VFS truncate/append, open/truncate/sync/close errors and missing-file safety passed");
    std::puts("Hidden SD write errors fail the current journal or GPX operation despite VFS success");
}
