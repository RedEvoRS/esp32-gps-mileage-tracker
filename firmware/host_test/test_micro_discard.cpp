#include <cassert>
#include <cstdio>
#include <cstring>
#include <string>
#include "micro_discard.h"

using namespace micro_discard;

static void testValidTripPath() {
    assert(validTripPath("/trips/t000001.gpx"));
    assert(validTripPath("/trips/a.gpx"));
    assert(!validTripPath(nullptr));
    assert(!validTripPath(""));
    assert(!validTripPath("/trips/.gpx"));
    assert(!validTripPath("/trips/t000001.gpx.bad"));
    assert(!validTripPath("/other/t000001.gpx"));
    assert(!validTripPath("/trips/sub/t000001.gpx"));
    assert(!validTripPath("/trips/../t000001.gpx"));
    assert(!validTripPath("/trips/a..b.gpx"));
    assert(!validTripPath("/trips/with space.gpx"));
    assert(!validTripPath("/trips/t000001\n.gpx"));
    std::string tooLong = "/trips/" + std::string(40, 'a') + ".gpx";
    assert(tooLong.size() >= kPathBytes);
    assert(!validTripPath(tooLong.c_str()));
}

static void testIncrementalHash() {
    const std::string data = "<?xml version=\"1.0\"?>\n<gpx><trk><name>Trip</name></trk></gpx>";
    const uint64_t whole = hashBytes(kHashOffset, data.data(), data.size());
    uint64_t chunked = kHashOffset;
    for (size_t i = 0; i < data.size(); i += 7) {
        size_t n = data.size() - i < 7 ? data.size() - i : 7;
        chunked = hashBytes(chunked, data.data() + i, n);
    }
    assert(whole == chunked);
    const std::string altered = "<?xml version=\"1.0\"?>\n<gpx><trk><name>Trip2</name></trk></gpx>";
    assert(whole != hashBytes(kHashOffset, altered.data(), altered.size()));
}

static void testListAndCodec() {
    PendingList list;
    assert(list.empty() && !list.hasActive());
    assert(!list.add(Backend::Sd, "/bad/path.gpx", 10, 1));
    assert(list.empty());

    assert(list.add(Backend::Sd, "/trips/t000001.gpx", 123, 0x1122334455667788ULL));
    assert(list.add(Backend::LittleFs, "/trips/t000001.gpx", 456, 0x8877665544332211ULL));
    assert(list.count() == 2);
    assert(list.contains(Backend::Sd, "/trips/t000001.gpx"));
    assert(list.contains(Backend::LittleFs, "/trips/t000001.gpx"));
    assert(!list.contains(Backend::Sd, "/trips/t000002.gpx"));

    // Updating an existing (backend, path) refreshes metadata and clears skipThisBoot.
    list.at(0).skipThisBoot = true;
    assert(!list.hasActive(Backend::Sd));
    assert(list.hasActive(Backend::LittleFs));
    assert(list.add(Backend::Sd, "/trips/t000001.gpx", 789, 0xAAAABBBBCCCCDDDDULL));
    assert(list.count() == 2);
    assert(list.hasActive(Backend::Sd));
    assert(list.at(0).size == 789 && list.at(0).hash == 0xAAAABBBBCCCCDDDDULL);

    // Roundtrip encode/decode preserves entries and resets RAM-only skipThisBoot.
    list.at(1).skipThisBoot = true;
    uint8_t blob[kMaxBlobBytes];
    const size_t len = list.encode(blob, sizeof(blob));
    assert(len == kHeaderBytes + 2 * kEntryBytes + kCrcBytes);
    PendingList decoded;
    assert(decoded.decode(blob, len));
    assert(decoded.count() == 2);
    assert(decoded.at(0).backend == Backend::Sd);
    assert(decoded.at(0).size == 789);
    assert(decoded.at(0).hash == 0xAAAABBBBCCCCDDDDULL);
    assert(std::strcmp(decoded.at(0).path, "/trips/t000001.gpx") == 0);
    assert(!decoded.at(0).skipThisBoot && !decoded.at(1).skipThisBoot);
    assert(decoded.at(1).backend == Backend::LittleFs && decoded.at(1).size == 456);

    // Corruption checks leave an existing list unchanged.
    PendingList intact = decoded;
    uint8_t bad[kMaxBlobBytes];
    std::memcpy(bad, blob, len);
    bad[len - 1] ^= 0x01; // CRC mismatch
    assert(!intact.decode(bad, len) && intact.count() == 2);

    std::memcpy(bad, blob, len);
    bad[0] = 'X'; // bad magic
    assert(!intact.decode(bad, len) && intact.count() == 2);

    assert(!intact.decode(blob, len - 1)); // truncated
    assert(!intact.decode(nullptr, len));

    // Corrupt backend byte with valid recomputed CRC is still rejected.
    std::memcpy(bad, blob, len);
    bad[kHeaderBytes] = 2; // invalid backend
    const uint32_t badCrc = crc32(bad, len - kCrcBytes);
    detail::putLe32(bad + len - kCrcBytes, badCrc);
    assert(!intact.decode(bad, len));

    // Path outside /trips/*.gpx with valid CRC is rejected.
    std::memcpy(bad, blob, len);
    bad[kHeaderBytes + 16] = 'X';
    detail::putLe32(bad + len - kCrcBytes, crc32(bad, len - kCrcBytes));
    assert(!intact.decode(bad, len));

    // Capacity eviction prefers a skipThisBoot entry, otherwise oldest slot 0.
    PendingList full;
    for (size_t i = 0; i < kMaxEntries; ++i) {
        char path[32];
        std::snprintf(path, sizeof(path), "/trips/t%06u.gpx", static_cast<unsigned>(i + 1));
        assert(full.add(Backend::Sd, path, static_cast<uint32_t>(100 + i), i + 1));
    }
    assert(full.count() == kMaxEntries);
    full.at(3).skipThisBoot = true;
    assert(full.add(Backend::Sd, "/trips/t000099.gpx", 999, 999));
    assert(full.count() == kMaxEntries);
    assert(!full.contains(Backend::Sd, "/trips/t000004.gpx"));
    assert(full.contains(Backend::Sd, "/trips/t000001.gpx"));
    assert(full.contains(Backend::Sd, "/trips/t000099.gpx"));

    // With no skipped entries, evicts oldest (slot 0).
    assert(full.add(Backend::Sd, "/trips/t000100.gpx", 1000, 1000));
    assert(!full.contains(Backend::Sd, "/trips/t000001.gpx"));
    assert(full.contains(Backend::Sd, "/trips/t000100.gpx"));

    // Max-capacity encode/decode roundtrip fits within kMaxBlobBytes.
    const size_t fullLen = full.encode(blob, sizeof(blob));
    assert(fullLen == kMaxBlobBytes);
    PendingList fullDecoded;
    assert(fullDecoded.decode(blob, fullLen) && fullDecoded.count() == kMaxEntries);
}

int main() {
    testValidTripPath();
    testIncrementalHash();
    testListAndCodec();
    std::puts("PASS micro_discard: path validation, incremental hash, capacity eviction and CRC codec");
    return 0;
}
