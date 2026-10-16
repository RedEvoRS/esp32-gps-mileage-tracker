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

flags=(-std=gnu++20 -O1 -w)
incs=(-I"$here/stubs" -I"$here/../04_trip_logger" -I"$build/include")
status=0

echo "== test_diag_catalog"
"${cxx[@]}" -std=c++14 -Wall -Wextra -pedantic "$here/test_diag_catalog.cpp" -o "$build/test_diag_catalog.exe"
"$build/test_diag_catalog.exe" || status=1

echo "== test_fs"
"${cxx[@]}" "${flags[@]}" "${incs[@]}" "$here/test_fs.cpp" -o "$build/test_fs.exe"
"$build/test_fs.exe" "$build/out" || status=1

tinygps="${TINYGPS_DIR:-$HOME/Documents/Arduino/libraries/TinyGPSPlus/src}"
echo "== test_gps"
if [ -f "$tinygps/TinyGPS++.cpp" ]; then
    # Real library dir first, so <TinyGPSPlus.h> resolves to it, not the stub.
    "${cxx[@]}" "${flags[@]}" -DARDUINO=100 -I"$tinygps" "${incs[@]}" \
        "$here/test_gps.cpp" "$tinygps/TinyGPS++.cpp" -o "$build/test_gps.exe"
    "$build/test_gps.exe" "$build/out" || status=1
else
    echo "SKIPPED: TinyGPSPlus not found (set TINYGPS_DIR to its src/ folder)"
fi
exit $status
