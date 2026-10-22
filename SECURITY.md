<!-- Support scope may change with the install/audience decision in #242. -->
# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open this repository's **Security**
tab and choose **Report a vulnerability**. Do not open a public issue for a
security problem or post exploit details publicly.

Include the affected firmware/server release tags or commit, relevant settings
(with secrets removed), steps to reproduce, expected and actual behaviour, and
the possible impact. A small proof of concept or sanitised log can help; omit
passwords, tokens, real trip coordinates and other personal information.

This is a single-maintainer hobby project. Reports and fixes are handled on a
best-effort basis, with no guaranteed response or fix time. The maintainer may
ask for reproduction details and discuss a fix and disclosure privately.

## Supported versions

Only the latest release of each component is supported:

- Firmware: the latest `fw-v*` release tag.
- Server: the latest `srv-v*` release tag.

Older releases receive no security fixes. The frozen `tbeam-v1.2` branch for the
retired T-Beam V1.2 receives no fixes or backports.

## Threat model: a trusted home network

The tracker and server are meant for a trusted home local area network (LAN).
Never expose the server's ports to the internet or forward them on your router.
Set `BIND_ADDR` in `server/.env` to the server's LAN IP address; otherwise Docker
publishes on all interfaces. Docker-published ports bypass UFW firewall rules.
Follow [deployment Step 7](DEPLOY_DEBIAN.md#step-7--firewall-and-network) to check
the actual bindings, including optional services.

The ingest service (port 8090) requires the `INGEST_AUTH_TOKEN` bearer token
(a shared upload secret) for trip and diagnostics uploads. The viewer (8091)
has an admin login in `viewer.env` and everyday logins in `annotations.db`.
The admin can remove or restore trips, manage logins, review and close years;
everyday logins can browse, annotate and export trips. The server checks the
current role on each request. Every private endpoint declares its permitted
roles; undeclared endpoints are denied. Passwords are stored as hashes. These controls
help keep casual LAN users without credentials from uploading data or viewing
trips. The viewer checks cross-site request forgery (CSRF) tokens as before,
and slows password guessing by locking a source IP for 15 minutes after five
failed logins.

Both services' `/health` endpoints are unauthenticated and report status,
service name and server version, without trip or device data. The viewer's
`/login` page and ingest's `/` service listing are also unauthenticated.
Optional monitor metrics (8093) require a separate bearer token; optional
self-hosted ntfy notifications (8092) deny anonymous access by default.

These controls do not encrypt traffic. Uploads, the viewer and metrics use plain
HTTP: anyone on the LAN who can sniff that traffic can read tokens, trip data,
login credentials and viewer sessions, and reuse stolen credentials. The
admin's session cookie also crosses the LAN over plain HTTP; this is one reason
trip removal stays reversible and leaves the source files intact. Cookies are
HttpOnly and SameSite=Lax, but are not Secure by default. Everyday password
hashes also live in `annotations.db` and are included in its backups; protect
those backups as trip data. See the existing
[TLS reverse-proxy note](server/README.md#security-notes); configuring a proxy
with Transport Layer Security (TLS) and its cookie setting is your responsibility.
It does not change the firmware's plain-HTTP upload connection.

The system does not protect against a compromised LAN device or server host.
Physical access to the tracker can expose the WiFi password, server address and
ingest token compiled into flash from `secrets.h`; that source file also holds
the secrets. The removable SD card stores trips without encryption. Trip data
is personal location data: keep data files, backups and logs private.

If you enable iOS instant notifications, the public ntfy.sh relay receives
message IDs and a SHA-256 hash of the notification topic URL, not alert text or
trip data; it can also observe the sending IP address and notification timing.
The phone fetches text from your own server and must be able to reach it. See
[deployment Step 11a](DEPLOY_DEBIAN.md#11a--start-and-secure-ntfy) and
[ntfy's iOS explanation](https://docs.ntfy.sh/config/#ios-instant-notifications).

## Out of scope

Problems that require internet exposure contrary to this setup are outside this
threat model. Third-party services you choose to add are outside this project's
support. Report vulnerabilities in Docker, the operating system or other
third-party software to their upstream maintainers.
