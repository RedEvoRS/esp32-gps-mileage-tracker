#pragma once
#include <cstdint>

namespace diagnostics {
// Mounted SD uses 512-byte sectors. Read its FAT BPB (superfloppy or primary
// MBR partition), rather than guessing a cluster size for quota accounting.
template<class Card> uint32_t journalSdCluster(Card& card, bool (*allow)() = nullptr) {
    uint8_t sector[512];
    auto le16 = [](const uint8_t* p) { return uint32_t(p[0]) | (uint32_t(p[1]) << 8); };
    auto bpb = [&](const uint8_t* p) -> uint32_t {
        uint32_t unit = p[13];
        if (p[510] != 0x55 || p[511] != 0xaa || le16(p + 11) != 512 ||
            !unit || (unit & (unit - 1)) || unit > 128 || !le16(p + 14) ||
            !p[16] || p[16] > 2) return 0;
        return 512 * unit;
    };
    if ((allow && !allow()) || !card.readRAW(sector, 0)) return 0;
    uint32_t unit = bpb(sector);
    if (unit) return unit;
    if (sector[510] != 0x55 || sector[511] != 0xaa) return 0;
    // SD's FAT mount selects a primary partition. Try each MBR entry; fail
    // closed on malformed media rather than exceed the allocated-byte quota.
    uint32_t starts[4] = {};
    for (unsigned i = 0; i < 4; ++i) {
        const uint8_t* p = sector + 446 + 16*i;
        if (p[4] == 1 || p[4] == 4 || p[4] == 6 || p[4] == 0x0b || p[4] == 0x0c || p[4] == 0x0e)
            starts[i] = le16(p + 8) | (le16(p + 10) << 16);
    }
    for (uint32_t start : starts) {
        if (allow && !allow()) return 0;
        if (start && card.readRAW(sector, start) && (unit = bpb(sector))) return unit;
    }
    return 0;
}
} // namespace diagnostics
