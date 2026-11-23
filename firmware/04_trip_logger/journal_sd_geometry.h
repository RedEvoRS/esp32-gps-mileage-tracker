#pragma once
#include <cstdint>
#include <cstring>

namespace diagnostics {
namespace sd_fat {
constexpr uint32_t kSectorBytes = 512;

inline uint32_t le16(const uint8_t* p) {
    return uint32_t(p[0]) | (uint32_t(p[1]) << 8);
}

inline uint32_t le32(const uint8_t* p) {
    return le16(p) | (le16(p + 2) << 16);
}

inline bool bootSignature(const uint8_t* sector) {
    return sector[510] == 0x55 && sector[511] == 0xaa;
}

inline bool basicBpb(const uint8_t* sector) {
    uint32_t unit = sector[13];
    return le16(sector + 11) == kSectorBytes && unit && !(unit & (unit - 1)) &&
           le16(sector + 14) && (sector[16] == 1 || sector[16] == 2);
}

inline bool fatBootSector(const uint8_t* sector) {
    // Same detection order as FatFs check_fs: FAT32 signature, then the
    // legacy BPB probe, which also accepts early FAT12/16 without a signature.
    if (sector[0] != 0xeb && sector[0] != 0xe9 && sector[0] != 0xe8) return false;
    if (bootSignature(sector) && std::memcmp(sector + 82, "FAT32   ", 8) == 0) return true;
    return basicBpb(sector) && le16(sector + 17) && le16(sector + 22) &&
           (le16(sector + 19) >= 4 || le32(sector + 32) >= 0x10000);
}

enum class BootSector { Found, Missing, ReadError };

template<class Read>
BootSector findBootSector(Read read, uint8_t* sector, uint32_t& start) {
    start = 0;
    if (!read(sector, 0)) return BootSector::ReadError;
    if (fatBootSector(sector)) return BootSector::Found;
    if (!bootSignature(sector)) return BootSector::Missing;

    // FatFs auto-selects the first recognizable primary partition and probes
    // every nonzero start LBA, regardless of the MBR's type byte (even 0x07).
    uint32_t starts[4];
    for (unsigned i = 0; i < 4; ++i) starts[i] = le32(sector + 454 + 16 * i);
    // FatFs find_volume() also moves on after a disk error on one entry, so
    // a stale or unreadable earlier entry must not condemn the volume it
    // actually mounted. Report a read error only when nothing was found.
    bool readFailed = false;
    for (uint32_t partition : starts) {
        if (!partition) continue;
        if (!read(sector, partition)) { readFailed = true; continue; }
        if (fatBootSector(sector)) {
            start = partition;
            return BootSector::Found;
        }
    }
    return readFailed ? BootSector::ReadError : BootSector::Missing;
}
} // namespace sd_fat

// Read the mounted FAT BPB rather than guessing a cluster size for quota
// accounting. Callers may cancel before any individual raw read.
template<class Card> uint32_t journalSdCluster(Card& card, bool (*allow)() = nullptr) {
    uint8_t sector[sd_fat::kSectorBytes];
    uint32_t start;
    auto read = [&](uint8_t* out, uint32_t lba) {
        return (!allow || allow()) && card.readRAW(out, lba);
    };
    return sd_fat::findBootSector(read, sector, start) == sd_fat::BootSector::Found &&
           sd_fat::basicBpb(sector) ? sd_fat::kSectorBytes * sector[13] : 0;
}
} // namespace diagnostics
