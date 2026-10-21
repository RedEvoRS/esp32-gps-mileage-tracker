#pragma once

// The sketch optionally includes the generated header before this file.
// CI requires it; Arduino IDE builds can still use the explicit fallback.
#ifndef FIRMWARE_VERSION_BASE
#ifdef FIRMWARE_VERSION_REQUIRE_STAMP
#error "Run python tools/stamp_firmware_version.py before compiling Stage 4"
#endif
#define FIRMWARE_VERSION_BASE "unversioned"
#endif

namespace firmware {
constexpr bool versionAlnum(char c) {
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
           (c >= '0' && c <= '9');
}
template <unsigned N> constexpr bool validVersion(const char (&text)[N]) {
    if (N < 2 || N > 33 || !versionAlnum(text[0]) || text[N - 1] != '\0') return false;
    for (unsigned i = 1; i < N - 1; ++i)
        if (!versionAlnum(text[i]) && text[i] != '.' && text[i] != '_' &&
            text[i] != '+' && text[i] != '-') return false;
    return true;
}
#if DISPLAY_DEBUG
constexpr char kVersion[] = FIRMWARE_VERSION_BASE "+dbg";
#else
constexpr char kVersion[] = FIRMWARE_VERSION_BASE;
#endif
static_assert(validVersion(kVersion), "Firmware version must be a protocol token of 1-32 characters");
} // namespace firmware
