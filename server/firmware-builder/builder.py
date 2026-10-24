"""Private settings, read-only enrollment, CI compilation and manifest helpers.

Stdlib only. Never print settings, compiler diagnostics, or exception details:
the compiler may repeat a source line containing credentials.
"""

import csv
from contextlib import closing
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile


DEVICE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}", re.ASCII)
BSSID = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", re.ASCII)
RECOVERY = ("See DIAGNOSTICS.md#snapshot-revision-enrollment-and-recovery. "
            "A reused card needs its SD lower bound; a used board needs a higher enrollment ID.")


def private_write(path, content):
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        os.chmod(path, 0o600)
        stream.write(content)


def write_json(path, value):
    private_write(path, json.dumps(value, indent=2) + "\n")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def env_value(path, key):
    """Read installer values without sourcing shell code or expanding dollars."""
    return parse_env(Path(path).read_text(encoding="utf-8"), key)


def parse_env(content, key):
    value = None
    for line in content.splitlines():
        match = re.fullmatch(r"\s*" + re.escape(key) + r"=(.*)", line)
        if match:
            value = match[1].strip()
    if value is None:
        return ""
    if value.startswith(("'", '"')):
        quote = value[0]
        if len(value) < 2 or value[-1] != quote:
            raise ValueError("Invalid quoting in .env; repair it with install.sh")
        value = value[1:-1]
        if quote == '"' and any(c in value for c in '$\\"'):
            raise ValueError("Unsupported .env expansion; use a plain installer-generated token")
    elif any(c.isspace() or c in "$#\\'\"" for c in value):
        raise ValueError("Unsupported .env syntax; use installer-generated values")
    return value


def validate_settings(settings):
    if not DEVICE.fullmatch(settings["device_id"]):
        raise ValueError("DEVICE_ID must match [a-z0-9][a-z0-9_-]{0,31}")
    if settings["bssid"] and not BSSID.fullmatch(settings["bssid"]):
        raise ValueError("BSSID must contain six colon-separated hexadecimal byte pairs")
    for key, minimum, maximum in (("ssid", 1, 32), ("password", 0, 64), ("token", 32, 512)):
        value = settings[key]
        if not minimum <= len(value.encode("utf-8")) <= maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError(f"Invalid {key} length or control character")
    host = settings["host"]
    try:
        address = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        if len(host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                      for label in host.split(".")):
            raise ValueError("Server host must be an IPv4 address or hostname, without URL or port")
    else:
        if address.is_unspecified or address.is_multicast:
            raise ValueError("Server host must be a reachable LAN address")
    if not re.fullmatch(r"[0-9]{1,5}", str(settings["port"])) or not 1 <= int(settings["port"]) <= 65535:
        raise ValueError("Server port must be 1..65535")


def c_string(value):
    # Fixed-width octal escapes preserve UTF-8 and cannot run into a following
    # hex digit or inject directives, quotes, backslashes, or newlines.
    return '"' + "".join(f"\\{byte:03o}" for byte in value.encode("utf-8")) + '"'


def secrets_header(settings, enroll=False, high_water=None):
    validate_settings(settings)
    lines = ["#pragma once", "#include <stdint.h>"]
    for key, define in (("ssid", "WIFI_SSID"), ("password", "WIFI_PASSWORD"),
                        ("host", "SERVER_HOST"), ("token", "AUTH_TOKEN"), ("device_id", "DEVICE_ID")):
        lines.append(f"#define {define} {c_string(settings[key])}")
    lines.append(f'#define SERVER_PORT {int(settings["port"])}')
    lines.append(f'#define USE_BSSID_FILTER {int(bool(settings["bssid"]))}')
    octets = settings["bssid"].split(":") if settings["bssid"] else ["00"] * 6
    lines.append("static const uint8_t WIFI_BSSID[6] = {" + ", ".join("0x" + x for x in octets) + "};")
    if enroll:
        if type(high_water) is not int or not 0 <= high_water <= (1 << 63) - 1:
            raise ValueError("Enrollment requires a verified server high-water")
        lines += ["#define DIAGNOSTICS_ENROLLMENT_ID 1ULL",
                  f"#define DIAGNOSTICS_STATUS_HIGH_WATER {high_water}ULL"]
    return "\n".join(lines) + "\n"


def ask(prompt, default="", secret=False):
    import termios  # Only interactive Linux builds need terminal controls.
    # A tty cannot seek; text-mode open(..., "r+") requires a seekable
    # buffered stream. Wrap the unbuffered device directly instead.
    with io.TextIOWrapper(io.FileIO("/dev/tty", "r+"), encoding="utf-8",
                          line_buffering=True, write_through=True) as tty:
        tty.write(prompt + (f" [{default}]" if default else "") + ": ")
        fd = tty.fileno()
        old = termios.tcgetattr(fd) if secret else None
        try:
            if secret:
                new = termios.tcgetattr(fd)
                new[3] &= ~termios.ECHO
                termios.tcsetattr(fd, termios.TCSADRAIN, new)
            answer = tty.readline()
            if not answer:
                raise ValueError("Terminal input ended")
        finally:
            if secret:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                tty.write("\n")
        return answer.rstrip("\r\n") or default


def yes(prompt, noninteractive, variable):
    if noninteractive:
        return os.environ.get(variable, "").lower() == "yes"
    return ask(prompt + " (yes/no)", "no").lower() == "yes"


def configure(install, private, noninteractive):
    install, private = Path(install), Path(private)
    if not (install / "firmware-src/firmware/04_trip_logger/04_trip_logger.ino").is_file():
        raise ValueError("Missing firmware-src/ bundle; run piece 1 first")
    print("The first run downloads about 1.8 GB of toolchain data (image about 2.7 GB; allow 10 GiB free for the first build); later builds reuse the image.", flush=True)
    if not yes("Continue with the toolchain download/build?", noninteractive, "TRACKER_ACCEPT_DOWNLOAD"):
        raise ValueError("Download not accepted (non-interactive: TRACKER_ACCEPT_DOWNLOAD=yes)")
    # Read once: the no-host-Python route supplies .env through a private FIFO.
    env = (install / ".env").read_text(encoding="utf-8")
    host = parse_env(env, "BIND_ADDR")
    if host in ("", "0.0.0.0", "::"):
        try:
            # Route lookup sends no packets. Documentation-only destination.
            route = json.loads(subprocess.check_output(["ip", "-j", "-4", "route", "get", "192.0.2.1"], text=True))
            host = route[0]["prefsrc"]
        except (OSError, subprocess.CalledProcessError, IndexError, KeyError, ValueError):
            host = ""
    settings = {"token": parse_env(env, "INGEST_AUTH_TOKEN")}
    for key, env, prompt, default, secret in (
            ("ssid", "TRACKER_WIFI_SSID", "2.4 GHz WiFi SSID", "", False),
            ("password", "TRACKER_WIFI_PASSWORD", "WiFi password (empty for open network)", "", True),
            ("bssid", "TRACKER_WIFI_BSSID", "Optional router BSSID (empty to skip)", "", False),
            ("device_id", "TRACKER_DEVICE_ID", "DEVICE_ID", "tbeam-01", False),
            ("host", "TRACKER_SERVER_HOST", "Confirm server LAN host", host, False),
            ("port", "TRACKER_SERVER_PORT", "Server ingest port", "8090", False)):
        if noninteractive:
            # Require explicit WiFi/device/host values; port and BSSID are optional.
            if key in ("ssid", "password", "device_id", "host") and env not in os.environ:
                raise ValueError(f"Missing required environment variable {env}")
            settings[key] = os.environ.get(env, default)
        else:
            settings[key] = ask(prompt, default, secret)
    validate_settings(settings)
    private_write(private / "normal.h", secrets_header(settings))
    write_json(private / "plan.json", {"device_id": settings["device_id"]})


def read_high_water(data, device):
    path = Path(data) / "diagnostics/events.db"
    # scandir must succeed: access failures are not evidence of an absent DB.
    entries = {p.name for p in Path(data).iterdir()}
    if "diagnostics" not in entries:
        return {"exists": False, "high_water": 0}
    entries = {p.name for p in path.parent.iterdir()}
    if "events.db" not in entries:
        return {"exists": False, "high_water": 0}
    # Do not use immutable=1: it can ignore committed snapshots in the WAL.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        row = db.execute("SELECT status_sequence FROM devices WHERE device_id=?", (device,)).fetchone()
        db.rollback()
    value = row[0] if row else None
    if value is not None and (type(value) is not int or not 0 <= value <= (1 << 63) - 1):
        raise ValueError("Invalid server high-water; investigate before enrollment")
    return {"exists": row is not None and value is not None, "high_water": value or 0}


def enrollment(private, noninteractive):
    private = Path(private)
    plan = read_json(private / "plan.json")
    state = read_json(private / "high-water.json")  # must exist after successful read
    enroll = False
    if not state["exists"]:
        fresh = yes("Is this SD card fresh/unused for this tracker?", noninteractive, "TRACKER_FRESH_SD")
        if fresh:
            enroll = yes("Has this board NEVER consumed any enrollment ID (ID 1 is unused)?",
                         noninteractive, "TRACKER_NEW_BOARD")
    plan["kind"] = "enroll" if enroll else "normal"
    if enroll:
        header = (private / "normal.h").read_text(encoding="utf-8")
        header += ("#define DIAGNOSTICS_ENROLLMENT_ID 1ULL\n"
                   f'#define DIAGNOSTICS_STATUS_HIGH_WATER {state["high_water"]}ULL\n')
        private_write(private / "enroll.h", header)
    else:
        print("Building normal firmware only. " + RECOVERY)
    write_json(private / "plan.json", plan)


def partition_limit(path):
    def number(value):
        value = value.strip().upper()
        return int(value[:-1], 0) * {"K": 1024, "M": 1024 * 1024}[value[-1]] if value.endswith(("K", "M")) else int(value, 0)
    with Path(path).open() as stream:
        rows = csv.reader(line.split("#", 1)[0] for line in stream)
        sizes = [number(row[4]) for row in rows if len(row) >= 5 and row[1].strip().lower() in ("app", "0", "0x00")]
    if not sizes or min(sizes) <= 0:
        raise ValueError("Partition table must contain a positive app partition")
    return min(sizes)


def write_manifest(output, build, exported, properties, plan, version, commit, fqbn, kind):
    """flash_args comes from core 3.3.11's postobjcopy.4 hook, not this code."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or kind not in ("normal", "enroll"):
        raise ValueError("Invalid build provenance")
    if not DEVICE.fullmatch(plan["device_id"]):
        raise ValueError("Invalid manifest device ID")
    lines = (Path(build) / "flash_args").read_text().splitlines()
    flags = shlex.split(lines[0])
    if len(flags) != 6 or flags[::2] != ["--flash-mode", "--flash-freq", "--flash-size"]:
        raise ValueError("Unexpected generated flash flags")
    project = properties["build.project_name"]
    files = {f"{project}.bootloader.bin": "bootloader", f"{project}.partitions.bin": "partitions",
             "boot_app0.bin": "boot_app0", f"{project}.bin": "app"}
    parts = []
    Path(output).mkdir(mode=0o700)
    for line in lines[1:]:
        offset, filename = shlex.split(line)
        if filename not in files or not re.fullmatch(r"0x[0-9A-Fa-f]+", offset):
            raise ValueError("Unexpected generated flash part")
        name = files.pop(filename)
        source = Path(exported) / filename
        if name == "boot_app0" and not source.exists():
            # CLI --output-dir exports the sketch .bin files, but not this
            # core support file. postobjcopy.5 copies it beside flash_args.
            source = Path(build) / filename
        if not source.is_file() or source.stat().st_size == 0:
            raise ValueError("Missing Arduino output part")
        target = Path(output) / f"{name}.bin"
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        parts.append({"name": name, "offset": offset, "file": target.name,
                      "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    if files:
        raise ValueError("Incomplete generated flash parts")
    manifest = {"schema": 1, "version": version, "commit": commit, "fqbn": fqbn,
                "chip": properties["build.mcu"], "flash_mode": flags[1], "flash_freq": flags[3],
                "flash_size": flags[5], "kind": kind, "device_id": plan["device_id"],
                "app_offset": next(p["offset"] for p in parts if p["name"] == "app"), "parts": parts}
    if manifest["chip"] != "esp32s3" or version.encode("ascii") not in (Path(output) / "app.bin").read_bytes():
        raise ValueError("App does not contain the stamped version or chip is wrong")
    write_json(Path(output) / "manifest.json", manifest)
    return manifest


def run_private(args, **kwargs):
    # Error output can contain secrets. Do not expose it even on compiler failure.
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    if result.returncode:
        raise ValueError("Build command failed: " + Path(args[0]).name + "; output withheld because it may contain credentials")
    return result.stdout.decode("utf-8")


def source_commit(root):
    # Git is authoritative in a checkout. A committed export-subst COMMIT file
    # can still contain its placeholder there; only archives use its contents.
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    commit = result.stdout.strip() if result.returncode == 0 else (Path(root) / "firmware/COMMIT").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected a full firmware source commit")
    return commit


def compile_images():
    plan = read_json("/private/plan.json")
    pins = dict(line.split("=", 1) for line in Path("/opt/builder/pins.env").read_text().splitlines() if line and not line.startswith("#"))
    with tempfile.TemporaryDirectory(prefix="firmware-") as temporary:
        root = Path(temporary) / "source"
        shutil.copytree("/source", root)
        os.environ["HOME"] = temporary
        # BuildKit's download-cache mount is absent at runtime. CLI initializes
        # a downloads directory even for an offline compile; keep it ephemeral.
        os.environ["ARDUINO_DIRECTORIES_DOWNLOADS"] = str(Path(temporary) / "downloads")
        # A complete checkout can be used for development; release bundles use
        # firmware/COMMIT through the shared stamp script.
        run_private(["git", "config", "--global", "--add", "safe.directory", str(root)])
        try:
            version = run_private(["python3", "tools/stamp_firmware_version.py"], cwd=root).strip()
        except ValueError:
            raise ValueError("Version stamping failed; use a complete Git checkout or a release with the archive-aware stamp script and firmware/COMMIT") from None
        commit = source_commit(root)
        if not re.fullmatch(r"[0-9a-f]{40}", commit) or not version.startswith((root / "firmware/VERSION").read_text().strip() + "+" + commit[:10]):
            raise ValueError("Firmware stamp and COMMIT disagree")
        sketch = root / "firmware/04_trip_logger"
        limit = partition_limit(sketch / "partitions.csv")
        variants = [("current", plan["kind"])] + ([("cleanup", "normal")] if plan["kind"] == "enroll" else [])
        for folder, kind in variants:
            shutil.copyfile(f"/private/{kind}.h", sketch / "secrets.h")
            build, exported = Path(temporary) / folder, Path(temporary) / (folder + "-export")
            args = ["arduino-cli", "compile", "--fqbn", pins["FQBN"], "--warnings", "default", "--jobs", "2",
                    "--build-property", f"upload.maximum_size={limit}", "--build-property",
                    "compiler.cpp.extra_flags=-DFIRMWARE_VERSION_REQUIRE_STAMP",
                    "--build-path", str(build), "--output-dir", str(exported), str(sketch)]
            props_text = run_private(args + ["--show-properties=expanded"])
            properties = dict(line.split("=", 1) for line in props_text.splitlines() if "=" in line)
            log = run_private(args)
            for line in log.splitlines():
                if line.startswith(("Sketch uses ", "Global variables use ")):
                    print(line, flush=True)
            write_manifest(Path("/result") / folder, build, exported, properties, plan, version, commit, pins["FQBN"], kind)
            print(f"Built {version}: {folder} ({kind})", flush=True)


def publish(install, private, prepare=False):
    install, private = Path(install), Path(private)
    result, output, previous = private / "result", install / "firmware-out", private / "previous"
    manifest = read_json(result / "current/manifest.json")
    summary = f'Firmware {manifest["version"]} ({manifest["kind"]}) is ready in firmware-out/current/.'
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(p.name not in ("current", "cleanup") for p in output.iterdir()))):
        raise ValueError("firmware-out must be a builder-owned directory containing only current/cleanup")
    # A fallback container validates read-only mounts; the host does the same
    # rename/rollback below without granting it write access to the install.
    if not prepare:
        if output.exists():
            output.rename(previous)
        try:
            result.rename(output)
        except OSError:
            if previous.exists():
                previous.rename(output)
            raise
    print(summary)
    print("Run piece 2 on the PC connected to the tracker (install/flash.ps1 or install/flash.sh).")
    if manifest["kind"] == "enroll":
        print("After Device health shows the first current snapshot, run piece 2 with -Cleanup / --cleanup; preserve NVS and trips.")


def main():
    os.umask(0o077)
    command, *args = sys.argv[1:]
    try:
        if command == "configure":
            configure(args[0], args[1], args[2] == "1")
        elif command == "high-water":
            plan = read_json(Path(args[1]) / "plan.json")
            write_json(Path(args[1]) / "high-water.json", read_high_water(args[0], plan["device_id"]))
        elif command == "enrollment":
            enrollment(args[0], args[1] == "1")
        elif command == "compile":
            compile_images()
        elif command == "publish":
            if len(args) != 2 and not (len(args) == 3 and args[2] == "--prepare"):
                raise ValueError("Expected publish INSTALL PRIVATE [--prepare]")
            publish(*args[:2], prepare=args[2:] == ["--prepare"])
        else:
            raise ValueError("Unknown builder operation")
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except (OSError, sqlite3.Error, KeyError, TypeError):
        raise SystemExit("Builder failed; settings/storage/build metadata could not be read. No enrollment value was inferred. " + RECOVERY) from None


if __name__ == "__main__":
    main()
