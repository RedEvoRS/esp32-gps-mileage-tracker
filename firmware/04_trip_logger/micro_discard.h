#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>

// Durable tracking for micro-trips whose remove() failed during endTrip().
// Pure C++14, fixed storage, no heap allocation. Each entry records the store,
// path, exact committed byte count and 64-bit FNV-1a content hash so a later
// retry or boot pass deletes only the exact discarded micro-trip and never
// touches a different file that happens to share the same filename.
namespace micro_discard {

enum class Backend : uint8_t { LittleFs = 0, Sd = 1 };
enum class Match : uint8_t { Matched, Mismatch, ReadError };

constexpr size_t kMaxEntries = 8;
constexpr size_t kPathBytes = 48;
constexpr size_t kHeaderBytes = 8;
constexpr size_t kEntryBytes = 64;
constexpr size_t kCrcBytes = 4;
constexpr size_t kMaxBlobBytes = kHeaderBytes + kMaxEntries * kEntryBytes + kCrcBytes;

constexpr uint64_t kHashOffset = 14695981039346656037ULL;
constexpr uint64_t kHashPrime = 1099511628211ULL;

inline uint64_t hashBytes(uint64_t state, const void* data, size_t len) {
    const auto* bytes = static_cast<const uint8_t*>(data);
    for (size_t i = 0; i < len; ++i) {
        state ^= bytes[i];
        state *= kHashPrime;
    }
    return state;
}

inline uint32_t crc32(const uint8_t* data, size_t len) {
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; ++i) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc & 1u) ? ((crc >> 1) ^ 0xEDB88320u) : (crc >> 1);
        }
    }
    return ~crc;
}

inline bool validTripPath(const char* path) {
    if (!path) return false;
    static constexpr char kPrefix[] = "/trips/";
    static constexpr char kSuffix[] = ".gpx";
    constexpr size_t kPrefixLen = sizeof(kPrefix) - 1;
    constexpr size_t kSuffixLen = sizeof(kSuffix) - 1;
    const size_t len = std::strlen(path);
    if (len <= kPrefixLen + kSuffixLen || len >= kPathBytes) return false;
    if (std::memcmp(path, kPrefix, kPrefixLen) != 0) return false;
    if (std::memcmp(path + len - kSuffixLen, kSuffix, kSuffixLen) != 0) return false;
    for (size_t i = kPrefixLen; i < len; ++i) {
        const unsigned char c = static_cast<unsigned char>(path[i]);
        if (c <= 0x20 || c >= 0x7F || c == '/' || c == '\\') return false;
        if (c == '.' && i + 1 < len && path[i + 1] == '.') return false;
    }
    return true;
}

struct Entry {
    Backend backend = Backend::LittleFs;
    uint32_t size = 0;
    uint64_t hash = kHashOffset;
    char path[kPathBytes] = {};
    // Boot-local only (not persisted): set when this store is present on this
    // boot and the path is either absent on removable media or holds different
    // content, avoiding repeated polls while preserving the record for media swap.
    bool skipThisBoot = false;
};

namespace detail {
inline void putLe32(uint8_t* p, uint32_t v) {
    p[0] = static_cast<uint8_t>(v & 0xFFu);
    p[1] = static_cast<uint8_t>((v >> 8) & 0xFFu);
    p[2] = static_cast<uint8_t>((v >> 16) & 0xFFu);
    p[3] = static_cast<uint8_t>((v >> 24) & 0xFFu);
}
inline void putLe64(uint8_t* p, uint64_t v) {
    putLe32(p, static_cast<uint32_t>(v & 0xFFFFFFFFu));
    putLe32(p + 4, static_cast<uint32_t>(v >> 32));
}
inline uint32_t getLe32(const uint8_t* p) {
    return static_cast<uint32_t>(p[0]) |
           (static_cast<uint32_t>(p[1]) << 8) |
           (static_cast<uint32_t>(p[2]) << 16) |
           (static_cast<uint32_t>(p[3]) << 24);
}
inline uint64_t getLe64(const uint8_t* p) {
    return static_cast<uint64_t>(getLe32(p)) |
           (static_cast<uint64_t>(getLe32(p + 4)) << 32);
}
} // namespace detail

class PendingList {
    Entry entries_[kMaxEntries];
    size_t count_ = 0;

public:
    void clear() { count_ = 0; }
    bool empty() const { return count_ == 0; }
    size_t count() const { return count_; }
    const Entry& at(size_t i) const { return entries_[i]; }
    Entry& at(size_t i) { return entries_[i]; }

    bool hasActive() const {
        for (size_t i = 0; i < count_; ++i) {
            if (!entries_[i].skipThisBoot) return true;
        }
        return false;
    }

    bool hasActive(Backend backend) const {
        for (size_t i = 0; i < count_; ++i) {
            if (entries_[i].backend == backend && !entries_[i].skipThisBoot) return true;
        }
        return false;
    }

    bool contains(Backend backend, const char* path) const {
        if (!path) return false;
        for (size_t i = 0; i < count_; ++i) {
            if (entries_[i].backend == backend && std::strcmp(entries_[i].path, path) == 0)
                return true;
        }
        return false;
    }

    const Entry* findActive(Backend backend, const char* path) const {
        if (!path) return nullptr;
        for (size_t i = 0; i < count_; ++i) {
            if (entries_[i].backend == backend && !entries_[i].skipThisBoot &&
                std::strcmp(entries_[i].path, path) == 0) {
                return &entries_[i];
            }
        }
        return nullptr;
    }

    bool hasActivePath(Backend backend, const char* path) const {
        return findActive(backend, path) != nullptr;
    }

    bool add(Backend backend, const char* path, uint32_t size, uint64_t hash) {
        if (!validTripPath(path)) return false;
        for (size_t i = 0; i < count_; ++i) {
            if (entries_[i].backend == backend && std::strcmp(entries_[i].path, path) == 0) {
                entries_[i].size = size;
                entries_[i].hash = hash;
                entries_[i].skipThisBoot = false;
                return true;
            }
        }
        if (count_ >= kMaxEntries) {
            size_t victim = 0;
            for (size_t i = 0; i < count_; ++i) {
                if (entries_[i].skipThisBoot) { victim = i; break; }
            }
            removeAt(victim);
        }
        Entry& slot = entries_[count_++];
        slot = Entry();
        slot.backend = backend;
        slot.size = size;
        slot.hash = hash;
        std::strncpy(slot.path, path, kPathBytes - 1);
        slot.path[kPathBytes - 1] = '\0';
        slot.skipThisBoot = false;
        return true;
    }

    void removeAt(size_t index) {
        if (index >= count_) return;
        for (size_t i = index + 1; i < count_; ++i) entries_[i - 1] = entries_[i];
        --count_;
    }

    size_t encode(uint8_t* out, size_t maxBytes) const {
        const size_t bodyLen = kHeaderBytes + count_ * kEntryBytes;
        const size_t totalLen = bodyLen + kCrcBytes;
        if (!out || maxBytes < totalLen) return 0;
        std::memset(out, 0, totalLen);
        out[0] = 'M'; out[1] = 'D'; out[2] = 'S'; out[3] = '1';
        out[4] = static_cast<uint8_t>(count_);
        for (size_t i = 0; i < count_; ++i) {
            uint8_t* p = out + kHeaderBytes + i * kEntryBytes;
            const Entry& e = entries_[i];
            const size_t pathLen = std::strlen(e.path);
            if (!validTripPath(e.path) || pathLen >= kPathBytes) return 0;
            p[0] = static_cast<uint8_t>(e.backend);
            p[1] = static_cast<uint8_t>(pathLen);
            detail::putLe32(p + 4, e.size);
            detail::putLe64(p + 8, e.hash);
            std::memcpy(p + 16, e.path, pathLen);
        }
        detail::putLe32(out + bodyLen, crc32(out, bodyLen));
        return totalLen;
    }

    bool decode(const uint8_t* data, size_t len) {
        if (!data || len < kHeaderBytes + kCrcBytes || len > kMaxBlobBytes) return false;
        if (data[0] != 'M' || data[1] != 'D' || data[2] != 'S' || data[3] != '1') return false;
        if (data[5] != 0 || data[6] != 0 || data[7] != 0) return false;
        const size_t n = data[4];
        if (n > kMaxEntries) return false;
        const size_t bodyLen = kHeaderBytes + n * kEntryBytes;
        if (len != bodyLen + kCrcBytes) return false;
        if (detail::getLe32(data + bodyLen) != crc32(data, bodyLen)) return false;

        PendingList parsed;
        for (size_t i = 0; i < n; ++i) {
            const uint8_t* p = data + kHeaderBytes + i * kEntryBytes;
            if (p[0] > static_cast<uint8_t>(Backend::Sd)) return false;
            const size_t pathLen = p[1];
            if (pathLen == 0 || pathLen >= kPathBytes) return false;
            if (p[2] != 0 || p[3] != 0) return false;
            char path[kPathBytes] = {};
            for (size_t j = 0; j < pathLen; ++j) {
                if (p[16 + j] == 0) return false;
                path[j] = static_cast<char>(p[16 + j]);
            }
            for (size_t j = pathLen; j < kPathBytes; ++j) {
                if (p[16 + j] != 0) return false;
            }
            const auto backend = static_cast<Backend>(p[0]);
            if (!validTripPath(path) || parsed.contains(backend, path)) return false;
            const uint32_t size = detail::getLe32(p + 4);
            const uint64_t hash = detail::getLe64(p + 8);
            if (!parsed.add(backend, path, size, hash)) return false;
        }
        *this = parsed;
        return true;
    }
};

} // namespace micro_discard
