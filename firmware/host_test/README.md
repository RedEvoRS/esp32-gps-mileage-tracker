# Host tests for the Stage 4 firmware

Runs the real `04_trip_logger.ino` on your PC against stubbed Arduino/ESP32
APIs, so the file-writing and trip-lifecycle logic can be tested without the
board.

```bash
bash firmware/host_test/run.sh
```

There are two suites:

- **`test_fs`**: file writing and the trip lifecycle. LittleFS is modeled
  faithfully (`stubs/arduino_stub.h`). Writes land in a 4 KB stdio buffer and
  report success, as they do on ESP32 core 3.x, and only `flush()`/`close()`
  commit them to the simulated flash. Tests can inject a failed open, a flash
  write error, or a full filesystem. The trip state machine is driven
  directly with synthetic fixes.
- **`test_gps`**: the GPS input path, built against the **real** TinyGPSPlus.
  It feeds the sketch's `loop()` the NMEA a NEO-M8N sends at its defaults,
  one sentence per call as the UART delivers it, and checks there's exactly
  one trackpoint per second. It also checks the UBX configuration frames
  sent at boot, including ACK, NAK and a silent receiver. Clock regressions
  cover carried RTC drift, real SNTP completion versus timeout, GPS correction
  after SNTP and every minute, stale GPS dates and midnight sentence ordering,
  empty RMC date/time fields, fractional seconds, and GPS arriving during an SNTP wait. The SNTP transport
  is simulated; these tests do not prove live network or multicore behavior.
  It looks for the
  library in the Arduino IDE's default folder, or wherever `TINYGPS_DIR`
  points, and is skipped if the library isn't found.

PMU and WiFi are compile-only stand-ins in both suites.

Trip files the tests leave behind are written to `build/out/`. To check that
they're valid, upload them to the ingest service the same way the curl test in
`server/README.md` uploads `sample.gpx`.

This doesn't replace the on-hardware shakedown. It proves the logic, not the
flash driver, the GPS, or the power behaviour.
