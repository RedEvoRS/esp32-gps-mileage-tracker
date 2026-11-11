#pragma once
#include "arduino_stub.h"
#include <cerrno>

// The same checked POSIX call sequence as production, over the trip disks.
// Operations commit immediately in this model; real power-loss durability
// and allocation/timing must still be tested on SD/FAT and LittleFS.
namespace trip_repair_io {
struct Handle { FakeDisk* disk; std::string path; size_t offset = 0; };
inline std::map<int, Handle> handles;
inline int nextFd = 100;
inline bool failed(FakeDisk& disk, const char* op) {
    disk.repairOperations.push_back(op);
    if (disk.repairFailOperation != op) return false;
    if (disk.repairFailuresToSkip) { --disk.repairFailuresToSkip; return false; }
    errno = EIO;
    return true;
}
inline int open(const char* full, int flags) {
    FakeDisk* disk = nullptr;
    const char* relative = nullptr;
    if (!std::strncmp(full, "/sd/", 4)) { disk = &g_sdDisk; relative = full + 3; }
    if (!std::strncmp(full, "/littlefs/", 10)) { disk = &g_disk; relative = full + 9; }
    if (!disk || !disk->mounted || disk->writesFail ||
        (flags & O_CREAT) || flags != O_WRONLY ||
        relative == disk->failOpenPath || !disk->files.count(relative) || failed(*disk, "open")) return -1;
    if (disk->openHandles) { std::fprintf(stderr, "repair reader still open\n"); std::abort(); }
    int fd = nextFd++;
    handles.emplace(fd, Handle{disk, relative}); ++disk->openHandles;
    return fd;
}
inline int ftruncate(int fd, off_t cut) {
    auto& h = handles.at(fd);
    if (failed(*h.disk, "truncate") || cut < 0 || static_cast<size_t>(cut) > h.disk->files.at(h.path).size()) return -1;
    h.disk->files.at(h.path).resize(static_cast<size_t>(cut));
    return 0;
}
inline int fsync(int fd) {
    return failed(*handles.at(fd).disk, "sync") ? -1 : 0;
}
inline off_t lseek(int fd, off_t offset, int whence) {
    auto& h = handles.at(fd);
    if (failed(*h.disk, "seek") || whence != SEEK_SET || offset < 0) return -1;
    h.offset = static_cast<size_t>(offset);
    return offset;
}
inline ssize_t write(int fd, const void* bytes, size_t size) {
    auto& h = handles.at(fd);
    if (failed(*h.disk, "write")) return -1;
    // Repair must position at the cut, never overwrite retained points.
    if (h.offset != h.disk->files.at(h.path).size()) { std::fprintf(stderr, "repair append at wrong offset\n"); std::abort(); }
    size_t room = h.disk->capacity > h.disk->used() ? h.disk->capacity - h.disk->used() : 0;
    size_t n = std::min(size, std::min(room, h.disk->repairWriteLimit));
    h.disk->files.at(h.path).append(static_cast<const char*>(bytes), n);
    h.offset += n;
    return static_cast<ssize_t>(n);
}
inline int close(int fd) {
    auto h = handles.at(fd); handles.erase(fd); --h.disk->openHandles;
    return failed(*h.disk, "close") ? -1 : 0;
}
}
