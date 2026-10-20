// Model a PSRAM-only workspace and capability-specific heap diagnostics.
#pragma once
#include "arduino_stub.h"
#include <cassert>
constexpr uint32_t MALLOC_CAP_SPIRAM = 1, MALLOC_CAP_8BIT = 2, MALLOC_CAP_INTERNAL = 4;
inline bool g_psramAvailable = true, g_psramAllocationFails = false;
inline unsigned g_psramAllocations = 0;
inline size_t g_psramAllocationBytes = 0;
inline uint32_t g_psramAllocationCaps = 0;
inline void* heap_caps_malloc(size_t bytes, uint32_t caps) {
    ++g_psramAllocations;
    g_psramAllocationBytes = bytes; g_psramAllocationCaps = caps;
    assert(caps == (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    return g_psramAvailable && !g_psramAllocationFails ? std::malloc(bytes) : nullptr;
}
inline size_t heap_caps_get_free_size(uint32_t caps) {
    if (caps == (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)) return 123456;
    assert(caps == MALLOC_CAP_SPIRAM);
    return g_psramAvailable ? 8123456 : 0;
}
inline size_t heap_caps_get_largest_free_block(uint32_t caps) {
    assert(caps == (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    return 65536;
}
