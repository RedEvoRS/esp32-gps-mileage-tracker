**Install**
- **Server first** (Debian 13, or Unraid; see `DEPLOY_DEBIAN.md` / `DEPLOY_UNRAID.md`):
  `curl -fsSL https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/setup.sh | bash`
  The server builds the firmware with your WiFi and upload token compiled in.
- **Then the tracker**, from a Windows PC: `irm https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/flash.ps1 | iex` (or `flash.sh` on Linux/macOS). The README walks through both steps.
- **Updating:** run both steps again and answer **no** to first install; trips and settings are kept.
- **No firmware binary is attached:** each tracker's WiFi password and upload token are compiled in. To build from source instead, see `firmware/README.md`.
