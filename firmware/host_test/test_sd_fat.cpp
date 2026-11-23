#include "../04_trip_logger/sd_fat_check.h"
#include "stubs/fake_sd_fat.h"
#include <array>
#include <cassert>
#include <cstdio>
#include <map>

using namespace fake_sd_fat;
using diagnostics::SdFatCheck;
using diagnostics::checkSdFat;

// Fake elapsed time: every raw read costs gReadCostMs.
static uint32_t gClockMs = 0, gReadCostMs = 0;
static uint32_t fakeNow() { return gClockMs; }

struct Card {
    std::map<uint32_t, std::array<uint8_t, 512>> overrides;
    uint32_t partition = 0;
    uint32_t unreadable = UINT32_MAX;
    unsigned reads = 0;

    bool readRAW(uint8_t* out, uint32_t lba) {
        ++reads;
        gClockMs += gReadCostMs;
        if (lba == unreadable) return false;
        auto it = overrides.find(lba);
        if (it != overrides.end()) std::memcpy(out, it->second.data(), 512);
        else if (lba < partition) std::memset(out, 0, 512);
        else sector(out, lba - partition);
        return true;
    }
    uint8_t* edit(uint32_t lba) {
        auto inserted = overrides.emplace(lba, std::array<uint8_t, 512>{});
        uint8_t* out = inserted.first->second.data();
        if (inserted.second && lba >= partition) sector(out, lba - partition);
        return out;
    }
    void fat(uint32_t cluster, uint32_t value) {
        put32(edit(partition + kFatStart + cluster / 128) + 4 * (cluster % 128), value);
    }
};

static void directory(uint8_t* entry, const char* name, uint32_t cluster, uint8_t attr = 0x10) {
    std::memcpy(entry, name, 11);
    entry[11] = attr;
    put16(entry + 20, uint16_t(cluster >> 16));
    put16(entry + 26, uint16_t(cluster));
}

int main() {
    Card fresh;
    assert(checkSdFat(fresh) == SdFatCheck::Passed && fresh.reads == 3);
    for (uint32_t flags : {0u, 0x04000000u, 0x08000000u, 0x0c000000u}) {
        Card windows;
        windows.fat(1, 0x03ffffff | flags);
        assert(checkSdFat(windows) == SdFatCheck::Passed);
    }
    Card fatfs;
    fatfs.fat(0, 0xfffffff8);
    fatfs.fat(1, 0xffffffff);
    fatfs.fat(2, 0xfffffff8);
    assert(checkSdFat(fatfs) == SdFatCheck::Passed);
    Card mkfsFat;
    mkfsFat.edit(0)[21] = 0xf0;
    mkfsFat.fat(0, 0x0ffffff0);
    mkfsFat.fat(1, 0x0ffffff8);
    assert(checkSdFat(mkfsFat) == SdFatCheck::Passed);

    Card incident;
    incidentFat(incident.edit(kFatStart));
    incidentFat(incident.edit(kFatStart + kFatSectors));
    assert(checkSdFat(incident) == SdFatCheck::ReservedEntries);
    assert(incident.reads == 2);
    std::memset(incident.edit(kFatStart), 0, 512);
    assert(checkSdFat(incident) == SdFatCheck::ReservedEntries);
    Card badFlags;
    badFlags.fat(1, 0x0ffffff7);
    assert(checkSdFat(badFlags) == SdFatCheck::ReservedEntries);

    Card directories;
    auto* root = directories.edit(kDataStart);
    directory(root, "TRIPS      ", 0, 0x0f); // LFN
    directory(root + 32, "TRIPS      ", 0);
    root[32] = 0xe5; // deleted
    directory(root + 64, "DIAG       ", 0, 0x20); // regular file
    directory(root + 96, "TRIPS      ", 7);
    directory(root + 128, "DIAG       ", 261);
    directory(root + 192, "DIAG       ", 0); // ignored after end marker
    directories.fat(7, 0x0fffffff);
    directories.fat(261, 15);
    directories.fat(15, 0x0ffffff8);
    assert(checkSdFat(directories) == SdFatCheck::Passed);
    for (uint32_t value : {0u, 1u, 0x0ffffff0u, 0x0ffffff7u, 973602u}) {
        Card rootBad = directories;
        rootBad.fat(2, value);
        assert(checkSdFat(rootBad) == SdFatCheck::RootChain);
        Card tripsBad = directories;
        tripsBad.fat(7, value);
        assert(checkSdFat(tripsBad) == SdFatCheck::TripsChain);
        Card diagBad = directories;
        diagBad.fat(15, value);
        assert(checkSdFat(diagBad) == SdFatCheck::DiagChain);
    }
    directory(root + 96, "TRIPS      ", 0);
    assert(checkSdFat(directories) == SdFatCheck::TripsChain);
    directory(root + 96, "TRIPS      ", 7);
    directories.fat(15, 15);
    assert(checkSdFat(directories) == SdFatCheck::DiagChain);
    Card loop;
    loop.fat(2, 3);
    loop.fat(3, 2);
    assert(checkSdFat(loop) == SdFatCheck::RootChain && loop.reads == 3);

    Card laterRoot;
    laterRoot.fat(2, 3);
    laterRoot.fat(3, 0x0fffffff);
    // Fill the first cluster, including deleted slots, so the lookup must
    // follow the FAT to a second cluster.
    for (uint32_t i = 0; i < kSectorsPerCluster; ++i)
        std::memset(laterRoot.edit(kDataStart + i), 0xe5, 512);
    directory(laterRoot.edit(clusterSector(3)), "DIAG       ", 15);
    assert(checkSdFat(laterRoot) == SdFatCheck::DiagChain);
    laterRoot.fat(15, 0x0fffffff);
    assert(checkSdFat(laterRoot) == SdFatCheck::Passed);
    for (uint32_t i = 0; i < kSectorsPerCluster; ++i)
        std::memset(laterRoot.edit(clusterSector(3) + i), 0xe5, 512);
    directory(laterRoot.edit(clusterSector(3) + kSectorsPerCluster - 1), "DIAG       ", 15);
    laterRoot.reads = 0;
    assert(checkSdFat(laterRoot) == SdFatCheck::Passed);
    assert(laterRoot.reads == diagnostics::sd_fat::kReadLimit);
    Card fullRootLoop = laterRoot;
    fullRootLoop.fat(3, 2);
    fullRootLoop.reads = 0;
    assert(checkSdFat(fullRootLoop) == SdFatCheck::RootChain);
    assert(fullRootLoop.reads == diagnostics::sd_fat::kReadLimit);

    // A fragmented, valid directory on a small-cluster volume can need more
    // FAT sector reads than the guard allows, even with a nearly empty root.
    Card longChain;
    longChain.edit(0)[13] = 1;
    put32(longChain.edit(0) + 32, kDataStart + 70000);
    directory(longChain.edit(kDataStart), "TRIPS      ", 128);
    for (uint32_t cluster = 128; cluster < 128 * 100; cluster += 128)
        longChain.fat(cluster, cluster + 128);
    longChain.fat(128 * 100, 0x0fffffff);
    assert(checkSdFat(longChain) == SdFatCheck::Passed);
    assert(longChain.reads == diagnostics::sd_fat::kReadLimit);
    // The final allowed read can still fail. Only a skipped read passes.
    longChain.reads = 0;
    longChain.unreadable = kFatStart + diagnostics::sd_fat::kReadLimit - 3;
    assert(checkSdFat(longChain) == SdFatCheck::ReadError);
    assert(longChain.reads == diagnostics::sd_fat::kReadLimit);

    Card mbr;
    mbr.partition = 2048;
    auto* table = mbr.edit(0);
    table[510] = 0x55;
    table[511] = 0xaa;
    put32(table + 454, 63); // non-FAT partition
    table[450 + 16] = 0x07; // FatFs probes the LBA, regardless of type
    put32(table + 454 + 16, mbr.partition);
    assert(checkSdFat(mbr) == SdFatCheck::Passed);
    assert(diagnostics::journalSdCluster(mbr) == 32768);
    // Like FatFs find_volume(), an unreadable earlier entry is skipped and
    // the volume FatFs actually mounts is the one checked.
    mbr.unreadable = 63;
    assert(checkSdFat(mbr) == SdFatCheck::Passed);
    mbr.fat(2, 1);
    assert(checkSdFat(mbr) == SdFatCheck::RootChain);
    mbr.fat(2, 0x0fffffff);
    mbr.unreadable = mbr.partition;
    assert(checkSdFat(mbr) == SdFatCheck::ReadError); // nothing readable found
    mbr.unreadable = UINT32_MAX;
    mbr.fat(2, 1);
    assert(checkSdFat(mbr) == SdFatCheck::RootChain);

    Card legacy;
    auto* boot = legacy.edit(0);
    std::memset(boot + 32, 0, 480);
    boot[13] = 4;
    put16(boot + 14, 1);
    put16(boot + 17, 512);
    put16(boot + 19, 32768);
    put16(boot + 22, 32);
    assert(checkSdFat(legacy) == SdFatCheck::Passed && legacy.reads == 1);
    put16(boot + 19, 2880); // FAT12, same legacy BPB layout
    assert(checkSdFat(legacy) == SdFatCheck::Passed);

    Card malformed;
    put32(malformed.edit(0) + 44, 1);
    assert(checkSdFat(malformed) == SdFatCheck::InvalidBpb);
    put32(malformed.edit(0) + 36, 0);
    assert(checkSdFat(malformed) == SdFatCheck::InvalidBpb);
    for (uint32_t lba : {0u, kFatStart, kDataStart}) {
        Card readFailure;
        readFailure.unreadable = lba;
        assert(checkSdFat(readFailure) == SdFatCheck::ReadError);
    }
    // ExtFlags: FatFs always writes the first FAT and mirrors it. Refuse a
    // non-mirrored volume whose active copy is another FAT.
    for (uint16_t flags : {uint16_t(0x0000), uint16_t(0x0080), uint16_t(0x0001)}) {
        Card mirrored;
        put16(mirrored.edit(0) + 40, flags);
        assert(checkSdFat(mirrored) == SdFatCheck::Passed);
    }
    Card secondActive;
    put16(secondActive.edit(0) + 40, 0x0081);
    assert(checkSdFat(secondActive) == SdFatCheck::InvalidBpb);

    // Elapsed-time budget: a slow but healthy card passes once time runs
    // out; damage seen before then is still reported.
    gClockMs = 0; gReadCostMs = 1500;
    Card slow = laterRoot; // needs dozens of reads
    slow.reads = 0;
    assert(checkSdFat(slow, fakeNow, 20000) == SdFatCheck::Passed);
    assert(slow.reads < diagnostics::sd_fat::kReadLimit && gClockMs < 20000 + 2 * 1500);
    Card slowIncident;
    incidentFat(slowIncident.edit(kFatStart));
    assert(checkSdFat(slowIncident, fakeNow, 20000) == SdFatCheck::ReservedEntries);
    Card expired; // budget gone before the FAT read: unverified, not damaged
    incidentFat(expired.edit(kFatStart));
    gReadCostMs = 30000;
    assert(checkSdFat(expired, fakeNow, 20000) == SdFatCheck::Passed);
    gReadCostMs = 0;
    std::puts("PASS SD FAT guard: formats, exact incident, chains, MBR, ExtFlags, read/time limits and I/O errors");
}
