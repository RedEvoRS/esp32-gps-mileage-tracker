# Review history

These four reports record reviews of this GPS mileage tracker's firmware
(the Arduino program on the board) and server. They are kept as written,
including the findings and fix status recorded at the time. Read them in order:

1. [Code review, 2026-06-09](CODE_REVIEW_2026-06-09.md): trip-logger concurrency and trip handling, upload and viewer checks, and fixes applied before deployment and hardware testing.
2. [Audit, 2026-08-04](AUDIT_2026-08-04.md): server security and exports, firmware storage and GPS logging, with reproduced server findings and recorded decisions.
3. [Audit, 2026-10-05](AUDIT_2026-10-05.md): re-check of August findings, new mileage and power-management findings, subsequent fixes, and V1.2 bench results.
4. [Independent audit, 2026-10-05](INDEPENDENT_AUDIT_2026-10-05.md): firmware data preservation, server upload durability and concurrency, viewer security, proposed fixes, and targeted regression tests.

All four cover the retired LilyGO T-Beam V1.2 (ESP32 and NEO-M8N GPS) and
the code as it was then. Its trips used a 2.375 MB internal-flash partition
with the LittleFS filesystem. The project now targets the T-Beam Supreme
(ESP32-S3 and MAX-M10S GPS), with microSD trip storage. The old pin,
power-rail and storage details do not apply to the Supreme. For example,
the AXP2101 power-management chip's ALDO3 rail powered GPS on the V1.2;
on the Supreme it powers the LoRa radio. Applying the old rail advice to
the Supreme can power the wrong device and risk radio damage without an antenna.

This repository starts from a clean snapshot taken on 2026-10-13. Earlier
commits, pull requests (PRs), issues and branch history remain in a private,
archived repository that public readers cannot open. Those references in
the reports will not resolve here.

For what is open, fixed or next, use this repository's
[GitHub issues](https://github.com/RedEvoRS/esp32-gps-mileage-tracker/issues).
Findings that carried over are tracked under labels such as
[`audit-2026-08-04`](https://github.com/RedEvoRS/esp32-gps-mileage-tracker/issues?q=label%3Aaudit-2026-08-04)
and [`audit-2026-10-05`](https://github.com/RedEvoRS/esp32-gps-mileage-tracker/issues?q=label%3Aaudit-2026-10-05).
