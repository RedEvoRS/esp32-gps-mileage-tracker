#include <cassert>
#include <cstdio>
#include <cstring>
#include "build_version.h"

static_assert(firmware::validVersion("unversioned"), "fallback token");
static_assert(firmware::validVersion("v0.05+0123456789-dirty"), "dirty token");
static_assert(firmware::validVersion("v99.99+0123456789-dirty"), "largest release dirty token");
static_assert(firmware::validVersion("A._+-9"), "allowed characters");
static_assert(firmware::validVersion("abcdefghijklmnopqrstuvwxyz012345"), "32 characters");
static_assert(!firmware::validVersion("abcdefghijklmnopqrstuvwxyz0123456"), "33 rejected");
static_assert(!firmware::validVersion(""), "empty rejected");
static_assert(!firmware::validVersion("-bad"), "leading punctuation rejected");
static_assert(!firmware::validVersion("a b"), "space rejected");
static_assert(!firmware::validVersion("a\nb"), "newline rejected");
static_assert(!firmware::validVersion("a\0b"), "embedded null rejected");
int main() {
    assert(std::strcmp(firmware::kVersion, FIRMWARE_VERSION_BASE) == 0);
    assert(firmware::validVersion(firmware::kVersion));
    std::printf("Build version: %s; token limits passed\n", firmware::kVersion);
}
