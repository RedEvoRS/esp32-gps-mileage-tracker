#pragma once
#include "journal_sd_geometry.h"

namespace sd_bench {
template<class Card> bool hasMarker(Card& card) {
    auto marker = card.open("/SDBENCH.OK", "r");
    const bool present = marker && !marker.isDirectory();
    marker.close();
    return present;
}

enum class HeaderResult { Passed, ReadError, Layout, ReservedEntries };
struct HeaderCheck {
    HeaderResult result;
    uint32_t copy = 0, fat0 = 0, fat1 = 0;
};

// Extra checks for this experiment. The production guard has already
// checked the BPB and directory chains. Require two FAT32 copies and the
// exact reserved entries (low 28 bits), including both flags in FAT[1].
template<class Card> HeaderCheck checkFatHeaders(Card& card) {
    using namespace diagnostics::sd_fat;
    uint8_t sector[kSectorBytes];
    uint32_t start;
    auto read = [&](uint8_t* out, uint32_t lba) { return card.readRAW(out, lba); };
    const auto boot = findBootSector(read, sector, start);
    if (boot == BootSector::ReadError) return {HeaderResult::ReadError};
    if (boot != BootSector::Found || !basicBpb(sector) || le16(sector + 17) ||
        le16(sector + 22) || sector[16] != 2 || sector[21] != 0xf8)
        return {HeaderResult::Layout};
    const uint32_t fatSize = le32(sector + 36);
    const uint64_t first = uint64_t(start) + le16(sector + 14);
    if (!fatSize || first + fatSize > UINT32_MAX) return {HeaderResult::Layout};
    for (uint32_t copy = 1; copy <= 2; ++copy) {
        if (!read(sector, uint32_t(first + (copy - 1) * fatSize)))
            return {HeaderResult::ReadError, copy};
        const uint32_t fat0 = le32(sector);
        const uint32_t fat1 = le32(sector + 4);
        if ((fat0 & 0x0fffffff) != 0x0ffffff8 || (fat1 & 0x0fffffff) != 0x0fffffff)
            return {HeaderResult::ReservedEntries, copy, fat0, fat1};
    }
    return {HeaderResult::Passed};
}
} // namespace sd_bench
