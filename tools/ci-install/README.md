# Two-piece CI lab

`audit-tests.yml` keeps the five existing check names. `installer` retains
the Ubuntu VM's source-copy install and its sudo, repair, concurrency, metrics
and stopped-service regression checks. Those cover behavior the bundle smoke
test does not replace. The same job then uses this disposable Debian 13 lab
for the release-bundle route. Both it and `release.yml` call
`tools/build_release_bundle.sh`; the latter still verifies the release tag's
commit before publishing.

The lab has systemd and a nested Docker daemon, no host Docker socket or
published ports. It runs setup fresh and again over a modified public file,
checks all private-path exclusions, checks service versions, and exercises
backup/check/restore with a populated synthetic file. Existing `install.sh`
checks supply upload, login, exports and backup-timer assertions. The lab
wrapper removes its container, anonymous volumes and lab image on exit.

Only relevant changes enable the expensive builder step. A plain Git diff
uses the PR base/head or push before/after SHAs; manual dispatch enables it
too. `firmware-compile` exports only its synthetic Stage 4 app and provenance,
with one-day artifact retention. Builder parity requires the same full commit,
FQBN and embedded version and permits a size difference of at most 1% or
4 KiB, whichever is larger. Different compiled settings prevent assuming
byte reproducibility. The actual Bash flash validator verifies the builder's
manifest, hashes, ranges and app-only selection without accessing hardware.
The existing builder unit suite enforces toolchain and compile-option pins.

The cache is an exact-key Docker image archive, keyed by public Dockerfile
and pins; inline build cache allows restoring its toolchain layers. Docker
still rebuilds the final builder-code layer against the current sources.
The builder labels images with a digest of every allowed context file and
can reuse an exact matching image after save/load without relying on the
Docker backend's build cache. A changed input always requires rebuilding.
No credentials, firmware output, install tree, or Docker daemon data
enter the cache. PR cache scope follows Actions' standard branch isolation;
there are no broad restore keys. `flash-windows` runs only for install changes,
installs a pinned analyzer and runs the existing
hardware-free PowerShell suite. **Keep this conditional job optional; do not
add it to branch protection.** Required names remain `pii-scan`, `server`,
`firmware-host`, `firmware-compile`, and `installer`.

Budget estimates, incremental to existing CI (runner time, not measurements):

- Unrelated PR: about 3–6 Linux minutes for bundle install/update/restore;
  no extra compiler run and no Windows runner.
- Relevant builder PR with warm cache: about 6–12 Linux minutes total extra.
- Cold builder cache: about 12–25 Linux minutes extra, including the 1–2 GB
  download, image save and cache upload. Install changes also add about
  1–2 Windows minutes (2–4 Linux-equivalent billed minutes).
- The installer timeout rises from 25 to 45 minutes; Windows has a 5-minute
  cap. A failed/hung run can consume the full caps. These are estimates to
  revisit after the first Actions run, especially for cache transfer speed.

Local entry point on a disposable Linux Docker host:

```bash
bash tools/build_release_bundle.sh ci /tmp/two-piece-assets
bash tools/ci-install/lab.sh /tmp/two-piece-assets /tmp/two-piece-cache /tmp/stage4-reference
# RUN_BUILDER=true also needs Stage 4's app + provenance in the reference dir.
```

Do not run the inner `run.sh` on a host or run a real install in WSL. On
Windows, use Docker Desktop to launch the lab. Bash syntax/lint and mocked
Python tests can run in WSL. Local Actions execution is not claimed.
