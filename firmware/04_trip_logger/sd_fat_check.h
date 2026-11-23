#pragma once
#include "journal_sd_geometry.h"

namespace diagnostics {
enum class SdFatCheck : uint8_t {
    Passed, ReadError, InvalidBpb, ReservedEntries, RootChain, TripsChain, DiagChain
};

inline const char* sdFatReason(SdFatCheck result) {
    switch (result) {
        case SdFatCheck::Passed: return "passed";
        case SdFatCheck::ReadError: return "read_error";
        case SdFatCheck::InvalidBpb: return "invalid_bpb";
        case SdFatCheck::ReservedEntries: return "fat_reserved";
        case SdFatCheck::RootChain: return "root_chain";
        case SdFatCheck::TripsChain: return "trips_chain";
        case SdFatCheck::DiagChain: return "diag_chain";
    }
    return "invalid_bpb";
}

namespace sd_fat {
// A boot guard, not a full filesystem scan. Bound I/O independently of the
// directory chain limit so an unusually large root cannot stall startup.
constexpr unsigned kReadLimit = 96;
constexpr uint32_t kFatMask = 0x0fffffff;
constexpr uint32_t kEndOfChain = 0x0ffffff8;
constexpr uint32_t kFirstReservedCluster = 0x0ffffff0;
constexpr uint32_t kDirectorySectors = (65536 * 32) / kSectorBytes;

template<class Card> class Checker {
public:
    explicit Checker(Card& card) : card_(card) {}

    SdFatCheck check() {
        uint32_t start;
        auto boot = findBootSector(
            [this](uint8_t* out, uint32_t lba) { return read(out, lba); }, data_, start);
        if (boot == BootSector::ReadError) return SdFatCheck::ReadError;
        if (boot != BootSector::Found || !basicBpb(data_)) return SdFatCheck::InvalidBpb;

        const uint32_t rootEntries = le16(data_ + 17);
        // A mounted FAT12/16 has a fixed root directory and a 16-bit FAT
        // size. Keep accepting it without applying any FAT32 checks.
        if (rootEntries && le16(data_ + 22)) return SdFatCheck::Passed;

        sectorsPerCluster_ = data_[13];
        const uint32_t reserved = le16(data_ + 14);
        const uint32_t fats = data_[16];
        const uint32_t fatSize = le32(data_ + 36);
        const uint32_t total = le16(data_ + 19) ? le16(data_ + 19) : le32(data_ + 32);
        const uint64_t dataOffset = reserved + uint64_t(fats) * fatSize;
        if (rootEntries || le16(data_ + 22) || le16(data_ + 42) || !fatSize ||
            dataOffset >= total || uint64_t(start) + total - 1 > UINT32_MAX)
            return SdFatCheck::InvalidBpb;
        const uint32_t clusters = (total - uint32_t(dataOffset)) / sectorsPerCluster_;
        lastCluster_ = clusters + 1;
        const uint32_t entriesPerSector = kSectorBytes / 4;
        const uint32_t root = le32(data_ + 44);
        if (clusters <= 0xfff5 || clusters > 0x0ffffff5 ||
            uint64_t(fatSize) * entriesPerSector < uint64_t(clusters) + 2 || !validCluster(root))
            return SdFatCheck::InvalidBpb;

        // FatFs uses the first FAT, even if ExtFlags selects another copy.
        // The incident damaged both copies, so checking equality adds no
        // protection here. FSInfo is only a hint and is deliberately ignored.
        fatStart_ = start + reserved;
        dataStart_ = start + uint32_t(dataOffset);
        const uint32_t mediaEntry = 0x0fffff00 | data_[21];
        uint32_t entry;
        if (!fatEntry(0, entry)) return SdFatCheck::ReadError;
        if (entry != mediaEntry) return SdFatCheck::ReservedEntries;
        if (!fatEntry(1, entry)) return SdFatCheck::ReadError;
        // FAT[1] can carry the clean-shutdown and hard-error flag bits.
        if ((entry | 0x0c000000) < kEndOfChain) return SdFatCheck::ReservedEntries;

        auto result = chain(root, SdFatCheck::RootChain);
        if (result != SdFatCheck::Passed || fatReadSkipped_) return result;
        if (tripsFound_) result = chain(trips_, SdFatCheck::TripsChain);
        if (result != SdFatCheck::Passed || fatReadSkipped_) return result;
        if (diagFound_) result = chain(diag_, SdFatCheck::DiagChain);
        return result;
    }

private:
    bool read(uint8_t* out, uint32_t lba) {
        ++reads_;
        return card_.readRAW(out, lba);
    }

    bool validCluster(uint32_t cluster) const {
        return cluster >= 2 && cluster <= lastCluster_ && cluster < kFirstReservedCluster;
    }

    bool fatEntry(uint32_t cluster, uint32_t& entry) {
        const uint32_t byte = cluster * 4;
        const uint32_t lba = fatStart_ + byte / kSectorBytes;
        if (lba != fatSector_) {
            if (reads_ == kReadLimit) {
                fatReadSkipped_ = true;
                return false;
            }
            if (!read(fat_, lba)) return false;
            fatSector_ = lba;
        }
        entry = le32(fat_ + byte % kSectorBytes) & kFatMask;
        return true;
    }

    bool scanRootCluster(uint32_t cluster) {
        const uint32_t start = dataStart_ + (cluster - 2) * sectorsPerCluster_;
        for (uint32_t i = 0; i < sectorsPerCluster_ && !rootScanned_ && reads_ < kReadLimit; ++i) {
            if (!read(data_, start + i)) return false;
            for (uint32_t offset = 0; offset < kSectorBytes; offset += 32) {
                const uint8_t* entry = data_ + offset;
                if (entry[0] == 0) {
                    rootScanned_ = true;
                    break;
                }
                // LFN and volume labels have bit 3 set, directories bit 4.
                if (entry[0] == 0xe5 || (entry[11] & 0x18) != 0x10) continue;
                if (!tripsFound_ && std::memcmp(entry, "TRIPS      ", 11) == 0) {
                    trips_ = (le16(entry + 20) << 16) | le16(entry + 26);
                    tripsFound_ = true;
                }
                if (!diagFound_ && std::memcmp(entry, "DIAG       ", 11) == 0) {
                    diag_ = (le16(entry + 20) << 16) | le16(entry + 26);
                    diagFound_ = true;
                }
                if (tripsFound_ && diagFound_) {
                    rootScanned_ = true;
                    break;
                }
            }
        }
        return true;
    }

    SdFatCheck chain(uint32_t cluster, SdFatCheck badChain) {
        const uint32_t limit = (kDirectorySectors + sectorsPerCluster_ - 1) / sectorsPerCluster_;
        for (uint32_t i = 0; i < limit; ++i) {
            if (!validCluster(cluster)) return badChain;
            uint32_t next;
            // At the I/O limit, keep following entries in the cached FAT
            // sector. A cache miss ends the check unless a read actually failed.
            if (!fatEntry(cluster, next))
                return fatReadSkipped_ ? SdFatCheck::Passed : SdFatCheck::ReadError;
            if (next < kEndOfChain && !validCluster(next)) return badChain;
            if (badChain == SdFatCheck::RootChain && !rootScanned_ &&
                reads_ < kReadLimit && !scanRootCluster(cluster))
                return SdFatCheck::ReadError;
            if (next >= kEndOfChain) return SdFatCheck::Passed;
            cluster = next;
        }
        return badChain; // Too long, including a loop within cached FAT sectors.
    }

    Card& card_;
    uint8_t data_[kSectorBytes], fat_[kSectorBytes];
    uint32_t fatSector_ = UINT32_MAX;
    uint32_t fatStart_, dataStart_, sectorsPerCluster_, lastCluster_;
    uint32_t trips_ = 0, diag_ = 0;
    unsigned reads_ = 0;
    bool rootScanned_ = false, tripsFound_ = false, diagFound_ = false;
    bool fatReadSkipped_ = false;
};
} // namespace sd_fat

template<class Card> SdFatCheck checkSdFat(Card& card) {
    return sd_fat::Checker<Card>(card).check();
}
} // namespace diagnostics
