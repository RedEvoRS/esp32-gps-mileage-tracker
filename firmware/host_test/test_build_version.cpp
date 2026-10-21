#include <cassert>
#include <cstdio>
#include <cstring>
#include "build_version.h"

static_assert(firmware::validVersion("unversioned"), "fallback token");
static_assert(firmware::validVersion("unversioned+dbg"), "debug fallback token");
static_assert(firmware::validVersion("0123456789-dirty+dbg"), "dirty debug token");
static_assert(firmware::validVersion("A._+-9"), "allowed characters");
static_assert(firmware::validVersion("abcdefghijklmnopqrstuvwxyz012345"), "32 characters");
static_assert(!firmware::validVersion("abcdefghijklmnopqrstuvwxyz0123456"), "33 rejected");
static_assert(!firmware::validVersion(""), "empty rejected");
static_assert(!firmware::validVersion("-bad"), "leading punctuation rejected");
static_assert(!firmware::validVersion("a b"), "space rejected");
static_assert(!firmware::validVersion("a\nb"), "newline rejected");
static_assert(!firmware::validVersion("a\0b"), "embedded null rejected");
int main() {
#if DISPLAY_DEBUG
    assert(std::strcmp(firmware::kVersion, FIRMWARE_VERSION_BASE "+dbg") == 0);
#else
    assert(std::strcmp(firmware::kVersion, FIRMWARE_VERSION_BASE) == 0);
#endif
    assert(firmware::validVersion(firmware::kVersion));
    std::printf("Build version: %s; token limits and flavour passed\n", firmware::kVersion);
}
