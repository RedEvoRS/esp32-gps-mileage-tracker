// This translation unit deliberately has no Arduino includes or host stubs.
#include "../04_trip_logger/diag_catalog.h"
#include <cassert>
#include <cstring>
#include <iostream>

using namespace diagnostics;
static_assert(kCatalogVersion == 1, "review the host fixture on catalog version changes");
static_assert(static_cast<std::uint16_t>(Code::STORAGE_FULL) == 4005, "stable code");
static_assert(find(4005)->severity == Severity::Critical, "constexpr lookup");
static_assert(find(5001)->journal == Journal::Counter, "no event for each absent scan");
static_assert(find(7999) == nullptr, "unknown codes need a display fallback");

int main() {
    assert(kCatalogSize == 44);
    for (std::size_t i = 0; i < kCatalogSize; ++i) {
        const auto& entry = kCatalog[i];
        assert(entry.oled_line_count >= 1 && entry.oled_line_count <= 3);
        assert(find(static_cast<std::uint16_t>(entry.code)) == &entry);
        for (std::size_t line = 0; line < 3; ++line) {
            assert(std::strlen(entry.oled[line]) <= 21);
        }
    }
    std::cout << "Diagnostic catalog: 44 entries passed\n";
}
