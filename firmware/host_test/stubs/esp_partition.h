// Raw partition reads are separate from the simulated mounted filesystem.
#pragma once
#include "arduino_stub.h"

using esp_err_t = int;
static constexpr esp_err_t ESP_OK = 0;
static constexpr int ESP_PARTITION_TYPE_DATA = 1;
static constexpr int ESP_PARTITION_SUBTYPE_ANY = 0xFF;
struct esp_partition_t { uint32_t size = 8192; };
struct PartitionStub {
    esp_partition_t partition;
    std::vector<uint8_t> bytes = std::vector<uint8_t>(8192, 0);
    bool missing = false;
    size_t failReadAt = SIZE_MAX;
    int finds = 0;
    size_t readBytes = 0;
    std::vector<size_t> readOffsets;
    bool wrongLookup = false;
};
inline PartitionStub g_partition;
inline const esp_partition_t* esp_partition_find_first(int type, int subtype, const char* label) {
    g_partition.finds++;
    g_partition.wrongLookup |= type != ESP_PARTITION_TYPE_DATA ||
        subtype != ESP_PARTITION_SUBTYPE_ANY || strcmp(label, "spiffs") != 0;
    return g_partition.missing ? nullptr : &g_partition.partition;
}
inline esp_err_t esp_partition_read(const esp_partition_t* partition, size_t offset,
                                    void* out, size_t n) {
    g_partition.readOffsets.push_back(offset);
    if (partition != &g_partition.partition || offset >= g_partition.failReadAt ||
        offset > g_partition.bytes.size() || n > g_partition.bytes.size() - offset) return -1;
    memcpy(out, g_partition.bytes.data() + offset, n);
    g_partition.readBytes += n;
    return ESP_OK;
}
