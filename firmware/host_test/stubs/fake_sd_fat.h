#pragma once
#include <cstdint>
#include <cstring>

namespace fake_sd_fat {
constexpr uint32_t kFatStart = 1170, kFatSectors = 7607, kDataStart = 16384;
constexpr uint8_t kSectorsPerCluster = 64;

inline void put16(uint8_t* p, uint16_t value) {
    p[0] = uint8_t(value);
    p[1] = uint8_t(value >> 8);
}
inline void put32(uint8_t* p, uint32_t value) {
    put16(p, uint16_t(value));
    put16(p + 2, uint16_t(value >> 16));
}
inline uint32_t clusterSector(uint32_t cluster) {
    return kDataStart + (cluster - 2) * kSectorsPerCluster;
}
inline void sector(uint8_t* data, uint32_t lba) {
    std::memset(data, 0, 512);
    if (lba == 0) {
        data[0] = 0xeb;
        data[2] = 0x90;
        put16(data + 11, 512);
        data[13] = kSectorsPerCluster;
        put16(data + 14, kFatStart);
        data[16] = 2;
        data[21] = 0xf8;
        put32(data + 32, kDataStart + 973600 * kSectorsPerCluster);
        put32(data + 36, kFatSectors);
        put32(data + 44, 2);
        std::memcpy(data + 82, "FAT32   ", 8);
        data[510] = 0x55;
        data[511] = 0xaa;
    } else if (lba == kFatStart || lba == kFatStart + kFatSectors) {
        put32(data, 0x0ffffff8);
        put32(data + 4, 0x0fffffff);
        put32(data + 8, 0x0fffffff);
    }
}

inline void incidentFat(uint8_t* data) {
    std::memset(data, 0, 512);
    put32(data + 4, 0x7fffffff);
    put32(data + 8, 1);
    put32(data + 24, 1);
    put32(data + 32, 0x1dd);
    put32(data + 60, 1);
    put32(data + 76, 0xc61c6c9e);
}
} // namespace fake_sd_fat
