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
wrapper removes its container, anonymous volumes and unique lab image on exit.
The nested Docker 29 image store uses native overlayfs on fresh anonymous
`/var/lib/docker` and `/var/lib/containerd` volumes. This avoids nesting
snapshots in the outer container's overlay and avoids `vfs` layer copies.
It retains Docker 29's containerd image store, as on the deployed server.

Before the lab, the VM stops the source-copy backup timer/service and removes
only that install's Compose project (all profiles, containers, volumes and
local service images), then prunes its completed build cache. It also removes
unused hosted .NET/Android/GHC/CodeQL/Boost toolsets on builder-enabled runs;
server-only runs avoid that extra cleanup. The job prints root filesystem
and Docker usage at startup, before the lab, and always after it,
including on failure. Source-copy and hosted-tool reclamation are measured
separately. Inside the lab, `LAB_DISK` records root used/available bytes and
the allocated Docker/containerd/cache footprint every ten seconds and at
phase boundaries. The final sample runs before removing the lab, even on
failure. Sampling uses `du -x` to exclude mounted executor rootfs views,
which would otherwise count image contents twice. These logs make subsequent
runner-image changes observable.

Builder parity stays in `installer`: after sequential project/cache cleanup,
a separate runner would still need the builder's own peak footprint. The
local cold run measured about 16 GiB incremental host use including the outer
lab image. Its allocated Docker/containerd/cache peak was 16,341,684,224
bytes (15.22 GiB); the gzip archive was 1,784,030,097 bytes (1.66 GiB).
Stable cache load/save and cache-hit reruns passed, peaking at 9.31 GiB and
9.13 GiB respectively. The 18 GiB budget also allows for the outer build
cache and sampling/runner variation. The job checks for **22 GiB free
immediately before the builder-enabled lab**, leaving an estimated 4 GiB margin. It
checks for 6 GiB on server-only runs. These are checks of actual `df` output,
not assumptions about a hosted image's starting space. If runner contents
change, a shortage fails clearly before creating the lab. Hosted-tool
reclamation and real runner headroom still need the next Actions run;
local Docker Desktop is not a measurement of the GitHub VM's free space.

Only relevant changes enable the expensive builder step. A plain Git diff
uses the PR base/head or push before/after SHAs; manual dispatch enables it
too. `firmware-compile` exports only its synthetic Stage 4 app and provenance,
with one-day artifact retention. Builder parity requires the same full commit,
FQBN and embedded version and permits a size difference of at most 1% or
4 KiB, whichever is larger. Different compiled settings prevent assuming
byte reproducibility. The actual Bash flash validator verifies the builder's
manifest, hashes, ranges and app-only selection without accessing hardware.
The existing builder unit suite enforces toolchain and compile-option pins.

The cache is an exact-key gzip Docker image archive, keyed by public Dockerfile
and pins; inline build cache allows restoring its toolchain layers. Docker
still rebuilds the final builder-code layer against the current sources.
The builder labels images with a digest of every allowed context file and
can reuse an exact matching image after save/load without relying on the
Docker backend's build cache. A changed input always requires rebuilding.
The restored archive is removed immediately after load. Server acceptance
finishes before its Compose images/volumes and BuildKit cache are removed;
then the builder runs. After compilation, its download mount and BuildKit
intermediates are pruned before streaming `docker save | gzip -1` on a cache
miss. There is no full uncompressed tar, and a cache hit skips saving again.
The tagged image is removed after validation/save. No credentials, firmware
output, install tree, or Docker daemon data enter the cache. PR cache scope
follows Actions' standard branch isolation;
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
Windows, use Git Bash with `MSYS_NO_PATHCONV=1` and Docker Desktop to launch
the lab; the wrapper converts bind paths to native Windows paths. Keep the
mounted checkout unchanged until the lab exits, because Bash reads these
scripts as it runs. Bash syntax/lint and mocked Python tests can run in WSL.
Local Actions execution is not claimed.
