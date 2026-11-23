#pragma once
#include "arduino_stub.h"
#include <cerrno>

// The same checked POSIX call sequence as production, over the trip disks.
// Writes immediately change the visible file size. Sync and close can still
// fail, as on a real filesystem with dirty data or metadata.
namespace trip_vfs_io {
struct Handle { FakeDisk* disk; std::string path; size_t offset; bool batch; bool append; };
inline std::map<int, Handle> handles;
inline int nextFd = 100;
inline bool failed(FakeDisk& disk, const char* op, bool batch = false) {
    if (batch) {
        if (disk.tripFailOperation != op) return false;
        if (disk.tripFailuresToSkip) { --disk.tripFailuresToSkip; return false; }
        disk.tripFailOperation.clear();
    } else {
        disk.repairOperations.push_back(op);
        if (disk.repairFailOperation != op) return false;
        if (disk.repairFailuresToSkip) { --disk.repairFailuresToSkip; return false; }
    }
    errno = EIO;
    return true;
}
inline int openHandle(const char* full, int flags, bool batch) {
    FakeDisk* disk = nullptr;
    const char* relative = nullptr;
    if (!std::strncmp(full, "/sd/", 4)) { disk = &g_sdDisk; relative = full + 3; }
    if (!std::strncmp(full, "/littlefs/", 10)) { disk = &g_disk; relative = full + 9; }
    if (!disk || !disk->mounted || disk->writesFail || relative == disk->failOpenPath) return -1;
    const bool create = flags == (O_WRONLY | O_CREAT | O_EXCL);
    const bool append = flags == (O_WRONLY | O_APPEND);
    if (!create && !append && flags != O_WRONLY) return -1;
    if (batch && disk->failNextOpens > 0) { --disk->failNextOpens; return -1; }
    const bool exists = disk->files.count(relative) != 0;
    // FatFs maps O_APPEND to FA_OPEN_ALWAYS, even without O_CREAT.
    const bool canCreate = create || (append && disk == &g_sdDisk);
    if ((create && exists) || (!exists && !canCreate) || failed(*disk, "open", batch)) return -1;
    if (!batch && disk->openHandles) { std::fprintf(stderr, "repair reader still open\n"); std::abort(); }
    if (!exists) { disk->mutatedDirectory(); disk->files.emplace(relative, ""); }
    int fd = nextFd++;
    handles.emplace(fd, Handle{disk, relative, 0, batch, append}); ++disk->openHandles;
    return fd;
}
inline int open(const char* full, int flags) { return openHandle(full, flags, false); }
inline int openBatch(const char* full, int flags, int = 0) { return openHandle(full, flags, true); }
inline int ftruncate(int fd, off_t cut) {
    auto& h = handles.at(fd);
    if (failed(*h.disk, "truncate") || cut < 0 || static_cast<size_t>(cut) > h.disk->files.at(h.path).size()) return -1;
    h.disk->files.at(h.path).resize(static_cast<size_t>(cut));
    return 0;
}
inline int fsync(int fd) {
    auto& h = handles.at(fd);
    return failed(*h.disk, "sync", h.batch) ? -1 : 0;
}
inline off_t lseek(int fd, off_t offset, int whence) {
    auto& h = handles.at(fd);
    if (h.batch) {
        if (failed(*h.disk, "seek", true) || whence != SEEK_END || offset != 0) return -1;
        h.offset = h.disk->files.at(h.path).size();
        return static_cast<off_t>(h.offset);
    }
    if (failed(*h.disk, "seek") || whence != SEEK_SET || offset < 0) return -1;
    h.offset = static_cast<size_t>(offset);
    return offset;
}
inline ssize_t write(int fd, const void* bytes, size_t size) {
    auto& h = handles.at(fd);
    if (!h.batch && failed(*h.disk, "write")) return -1;
    auto& file = h.disk->files.at(h.path);
    if (h.append) h.offset = file.size();
    if (h.offset > file.size() || (h.batch && h.offset != file.size())) {
        std::fprintf(stderr, "write at wrong offset\n");
        std::abort();
    }
    size_t overwrite = file.size() - h.offset;
    size_t room = h.disk->capacity > h.disk->used() ? h.disk->capacity - h.disk->used() : 0;
    size_t n = std::min(size, room + overwrite);
    if (!h.batch) n = std::min(n, h.disk->repairWriteLimit);
    ++h.disk->writeCalls;
    file.replace(h.offset, std::min(n, overwrite), static_cast<const char*>(bytes), n);
    h.offset += n;
    // A write can modify bytes before the driver returns an error.
    if (h.batch && failed(*h.disk, "write", true)) return -1;
    return static_cast<ssize_t>(n);
}
inline int close(int fd) {
    auto h = handles.at(fd); handles.erase(fd); --h.disk->openHandles;
    return failed(*h.disk, "close", h.batch) ? -1 : 0;
}
}
