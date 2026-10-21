// This translation unit deliberately has no Arduino includes or host stubs.
#include "../04_trip_logger/diag_catalog.h"
#include <cassert>
#include <cstring>
#include <iostream>

using namespace diagnostics;
static_assert(kCatalogVersion == 6, "review the host fixture on catalog version changes");
static_assert(find(2009)->severity == Severity::Info && !find(2009)->notify, "battery history only");
static_assert(find(2009)->journal == Journal::PerOccurrence, "power-off is a notice");
static_assert(static_cast<std::uint16_t>(Code::STORAGE_FULL) == 4005, "stable code");
static_assert(find(4005)->severity == Severity::Critical, "constexpr lookup");
static_assert(find(5001)->journal == Journal::Counter, "no event for each absent scan");
static_assert(static_cast<std::uint16_t>(Code::PMU_STARTUP_RECOVERY) == 2007, "stable power code");
static_assert(find(2007)->severity == Severity::Warning && !find(2007)->notify, "history without paging");
static_assert(find(2007)->journal == Journal::PerOccurrence, "startup recovery is a notice");
static_assert(static_cast<std::uint16_t>(Code::CHARGE_CONTROL_FAILED) == 2008, "stable power code");
static_assert(find(2008)->severity == Severity::Error && find(2008)->notify, "charge control failure eligible");
static_assert(find(2008)->journal == Journal::Transitions, "confirmed control recovery clears failure");
static_assert(static_cast<std::uint16_t>(Code::GPS_ANTENNA_SUSPECT) == 3009, "stable GPS code");
static_assert(find(3009)->severity == Severity::Warning && find(3009)->notify, "antenna warning eligible");
static_assert(find(3009)->journal == Journal::Transitions, "antenna condition");
static_assert(find(7999) == nullptr, "unknown codes need a display fallback");

int main() {
    assert(kCatalogSize == 48);
    for (std::size_t i = 0; i < kCatalogSize; ++i) {
        const auto& entry = kCatalog[i];
        assert(entry.oled_line_count >= 1 && entry.oled_line_count <= 3);
        assert(find(static_cast<std::uint16_t>(entry.code)) == &entry);
        for (std::size_t line = 0; line < 3; ++line) {
            assert(std::strlen(entry.oled[line]) <= 21);
        }
    }
    std::cout << "Diagnostic catalog: 48 entries passed\n";
}
