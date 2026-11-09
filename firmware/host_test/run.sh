#!/usr/bin/env bash
# Build and run the host-side tests for 04_trip_logger.ino.
#
# Needs a C++20 compiler: $CXX if set, else g++ or clang++ on PATH, else
# zig's bundled clang (`pip install ziglang`). Trip files the tests produce
# are left in build/out/ so they can be fed to the ingest service.
#
# test_gps builds against the real TinyGPSPlus library: $TINYGPS_DIR, or the
# Arduino IDE's default library folder. It's skipped if neither exists.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
build="$here/build"
mkdir -p "$build/include" "$build/out"

# The sketch includes "secrets.h", which is never committed. The tests don't
# use its values, so the example file stands in when the real one is absent.
cp "$here/../secrets.h.example" "$build/include/secrets.h"

if [ -n "${CXX:-}" ]; then cxx=($CXX)
elif command -v g++ >/dev/null 2>&1; then cxx=(g++)
elif command -v clang++ >/dev/null 2>&1; then cxx=(clang++)
elif python -m ziglang version >/dev/null 2>&1; then cxx=(python -m ziglang c++)
else
    echo "No C++ compiler found: set CXX, install g++/clang++, or pip install ziglang" >&2
    exit 2
fi

# Optimizing compilers (including zig c++) may define NDEBUG implicitly.
# Apply these flags to EVERY build, including the C++14-only tests below.
assert_flags=(-UNDEBUG -include "$here/assertions_enabled.h")
flags=("${assert_flags[@]}" -std=gnu++20 -O1 -Wall -Wextra -Werror -DJOURNAL_HOST_TEST)
project_incs=(-I"$here/../04_trip_logger" -I"$build/include" -I"$here/display_stubs")
incs=(-I"$here/stubs" "${project_incs[@]}")
status=0
# Each test binary runs twice; HOST_DEBUG is read only by host stubs.
run_modes() {
    HOST_DEBUG=0 "$@" || status=1
    HOST_DEBUG=1 "$@" || status=1
}

for base in fallback stamped; do
    echo "== test_build_version ($base)"
    version_args=()
    if [ "$base" = stamped ]; then
        version_args=('-DFIRMWARE_VERSION_BASE="v0.05+0123456789-dirty"' -DFIRMWARE_VERSION_REQUIRE_STAMP)
    fi
    "${cxx[@]}" "${assert_flags[@]}" -std=c++14 -Wall -Wextra -Werror -pedantic \
        "${version_args[@]}" -I"$here/../04_trip_logger" \
        "$here/test_build_version.cpp" -o "$build/test_build_version.exe"
    "$build/test_build_version.exe" || status=1
done

echo "== test_temperature (Bosch compensation and checked debug probes)"
"${cxx[@]}" "${assert_flags[@]}" -std=c++14 -O1 -Wall -Wextra -Werror -pedantic \
    "$here/test_temperature.cpp" -o "$build/test_temperature.exe"
"$build/test_temperature.exe" || status=1

echo "== test_degraded_policy"
"${cxx[@]}" "${assert_flags[@]}" -std=c++14 -Wall -Wextra -Werror -pedantic "$here/test_degraded_policy.cpp" -o "$build/test_degraded_policy.exe"
"$build/test_degraded_policy.exe" || status=1

echo "== test_rtc (pure calendar and clock confidence)"
"${cxx[@]}" "${assert_flags[@]}" -std=c++14 -O1 -Wall -Wextra -Werror "${incs[@]}" "$here/test_rtc.cpp" -o "$build/test_rtc.exe"
"$build/test_rtc.exe" || status=1

echo "== test_gps_antenna"
"${cxx[@]}" "${assert_flags[@]}" -std=c++14 -Wall -Wextra -Werror -pedantic \
    "$here/test_gps_antenna.cpp" -o "$build/test_gps_antenna.exe"
"$build/test_gps_antenna.exe" || status=1

echo "== test_debug_display (bus fault injection)"
"${cxx[@]}" "${flags[@]}" -I"$here/display_stubs" -I"$here/../04_trip_logger" \
    "$here/test_debug_display.cpp" -o "$build/test_debug_display.exe"
"$build/test_debug_display.exe" || status=1

for test in test_display_model test_production_display_layout test_production_display; do
    echo "== $test"
    "${cxx[@]}" "${assert_flags[@]}" -std=c++14 -Wall -Wextra -Werror -pedantic \
        -I"$here/display_stubs" -I"$here/../04_trip_logger" "$here/$test.cpp" -o "$build/$test.exe"
    "$build/$test.exe" || status=1
done

echo "== test_display_model (DISPLAY_CARD_MS=10000)"
"${cxx[@]}" "${assert_flags[@]}" -std=c++14 -Wall -Wextra -Werror -pedantic \
    -DDISPLAY_CARD_MS=10000 -I"$here/../04_trip_logger" \
    "$here/test_display_model.cpp" -o "$build/test_display_model_10s.exe"
"$build/test_display_model_10s.exe" || status=1

u8g2="${U8G2_DIR:-$HOME/Documents/Arduino/libraries/U8g2}"
if [ -f "$u8g2/src/clib/u8g2_fonts.c" ]; then
    "${PYTHON:-python}" "$here/check_display_fonts.py" "$u8g2" || status=1
else
    echo "SKIPPED: installed-font comparison (set U8G2_DIR); layout tests use extracted metrics"
fi

echo "== test_diag_catalog"
"${cxx[@]}" "${assert_flags[@]}" -std=c++14 -Wall -Wextra -Werror -pedantic "$here/test_diag_catalog.cpp" -o "$build/test_diag_catalog.exe"
"$build/test_diag_catalog.exe" || status=1

echo "== test_pmu_startup_event"
"${cxx[@]}" "${flags[@]}" "${incs[@]}" "$here/test_pmu_startup_event.cpp" -o "$build/test_pmu_startup_event.exe"
"$build/test_pmu_startup_event.exe" || status=1

echo "== test_event_log"
"${cxx[@]}" "${assert_flags[@]}" -std=c++14 -O1 -Wall -Wextra -Werror -pedantic -pthread "${incs[@]}" \
    "$here/test_event_log.cpp" "$here/../04_trip_logger/event_log.cpp" -o "$build/test_event_log.exe"
"$build/test_event_log.exe" || status=1
"${PYTHON:-python}" "$here/check_event_log_protocol.py" "$build/test_event_log.exe" || status=1

echo "== test_journal_faults (E4008 accounting, recovery and bounded feedback)"
"${cxx[@]}" "${assert_flags[@]}" -std=c++14 -O1 -Wall -Wextra -Werror -pedantic -pthread "${incs[@]}" \
    "$here/test_journal_faults.cpp" "$here/../04_trip_logger/event_log.cpp" -o "$build/test_journal_faults.exe"
"$build/test_journal_faults.exe" || status=1

echo "== test_fs"
"${cxx[@]}" "${flags[@]}" "${incs[@]}" "$here/test_fs.cpp" "$here/../04_trip_logger/event_log.cpp" -o "$build/test_fs.exe"
run_modes "$build/test_fs.exe" "$build/out"

echo "== test_sd_runtime (removed SD classification and between-trip fallback)"
"${cxx[@]}" "${flags[@]}" "${incs[@]}" "$here/test_sd_runtime.cpp" \
    "$here/../04_trip_logger/event_log.cpp" -o "$build/test_sd_runtime.exe"
run_modes "$build/test_sd_runtime.exe"

echo "== test_journal (Stage 4 mounted adapter/service integration)"
"${cxx[@]}" "${flags[@]}" "${incs[@]}" "$here/test_journal.cpp" \
    "$here/../04_trip_logger/event_log.cpp" -o "$build/test_journal.exe"
run_modes "$build/test_journal.exe"

echo "== test_battery_events (checked samples and shutdown durability)"
"${cxx[@]}" "${flags[@]}" "${incs[@]}" "$here/test_battery_events.cpp" \
    "$here/../04_trip_logger/event_log.cpp" -o "$build/test_battery_events.exe"
run_modes "$build/test_battery_events.exe"
run_modes "${PYTHON:-python}" "$here/check_battery_events_protocol.py" "$build/test_battery_events.exe"

echo "== test_journal_vfs (checked mounted VFS operations)"
"${cxx[@]}" "${flags[@]}" -I"$here/../04_trip_logger" "$here/test_journal_vfs.cpp" \
    -o "$build/test_journal_vfs.exe"
vfs_root="$(mktemp -d -t diag-vfs-XXXXXX)"
"$build/test_journal_vfs.exe" "$vfs_root" || status=1

echo "== test_event_upload (production worker, protocol and local ingest)"
"${cxx[@]}" "${flags[@]}" "${incs[@]}" "$here/test_event_upload.cpp" \
    "$here/../04_trip_logger/event_log.cpp" -o "$build/test_event_upload.exe"
run_modes "$build/test_event_upload.exe"
"$build/test_event_upload.exe" --no-psram || status=1
"$build/test_event_upload.exe" --alloc-failure || status=1
"${PYTHON:-python}" "$here/check_event_upload_protocol.py" "$build/test_event_upload.exe" || status=1

# Each sketch carries its own recovery header; exercise every copy.
for sketch in 02_pmu_test 03_gps_test 04_trip_logger; do
    echo "== test_i2c ($sketch)"
    "${cxx[@]}" "${flags[@]}" -I"$here/../$sketch" "${incs[@]}" \
        "$here/test_i2c.cpp" -o "$build/test_i2c_$sketch.exe"
    "$build/test_i2c_$sketch.exe" || status=1
done

tinygps="${TINYGPS_DIR:-$HOME/Documents/Arduino/libraries/TinyGPSPlus/src}"
echo "== test_gps"
if [ -f "$tinygps/TinyGPS++.cpp" ]; then
    # Keep project stubs warning-checked, but omit the compile-only GPS stub
    # so the real library can be a system include without being shadowed.
    gps_stubs="$build/gps-stubs"
    mkdir -p "$gps_stubs"
    for header in "$here/stubs/"*.h; do
        if [ "$(basename "$header")" != TinyGPSPlus.h ]; then cp "$header" "$gps_stubs/"; fi
    done
    gps_incs=(-isystem "$tinygps" -I"$gps_stubs" "${project_incs[@]}")
    # Compile upstream implementation as a system include too: TinyGPSPlus
    # 1.0.3 has an intentional switch fallthrough without a GCC annotation.
    # Project sources and stubs still build with the same strict flags.
    tinygps_source="$build/tinygps_library.cpp"
    printf '#include <TinyGPS++.cpp>\n' > "$tinygps_source"
    "${cxx[@]}" "${flags[@]}" -DARDUINO=100 "${gps_incs[@]}" \
        -c "$tinygps_source" -o "$build/tinygps_library.o"
    "${cxx[@]}" "${flags[@]}" -DARDUINO=100 "${gps_incs[@]}" \
        "$here/test_gps.cpp" "$here/../04_trip_logger/event_log.cpp" "$build/tinygps_library.o" -o "$build/test_gps.exe"
    HOST_DEBUG=0 "$build/test_gps.exe" "$build/out" || status=1
    mkdir -p "$build/out-debug"
    HOST_DEBUG=1 "$build/test_gps.exe" "$build/out-debug" || status=1
    for file in "$build/out-debug/"*.gpx; do
        cmp "$file" "$build/out/$(basename "$file")" || status=1
    done
    for test in test_loop_runtime test_temperature_runtime test_degraded test_rtc_runtime test_display_runtime test_fault_hooks test_pmu_verification; do
        echo "== $test (single binary, runtime modes)"
        "${cxx[@]}" "${flags[@]}" -DARDUINO=100 "${gps_incs[@]}" \
            "$here/$test.cpp" "$here/../04_trip_logger/event_log.cpp" "$build/tinygps_library.o" -o "$build/$test.exe"
        run_modes "$build/$test.exe"
    done
    run_modes "${PYTHON:-python}" "$here/check_rtc_protocol.py" "$build/test_rtc_runtime.exe" || status=1
    run_modes "${PYTHON:-python}" "$here/check_fault_hooks_protocol.py" "$build/test_fault_hooks.exe" || status=1
    echo "== test_debug_status (formatting and real GSV parser)"
    "${cxx[@]}" "${flags[@]}" -DARDUINO=100 "${gps_incs[@]}" \
        "$here/test_debug_status.cpp" "$build/tinygps_library.o" -o "$build/test_debug_status.exe"
    "$build/test_debug_status.exe" || status=1
else
    echo "SKIPPED: TinyGPSPlus not found (set TINYGPS_DIR to its src/ folder)"
fi
exit $status
