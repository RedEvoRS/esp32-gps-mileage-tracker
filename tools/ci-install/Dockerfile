# Disposable Debian 13 server, with its own Docker daemon and systemd.
# No host Docker socket, host ports, or pre-existing volumes are used.
FROM debian:13.1
ENV container=docker
RUN apt-get update && apt-get install -y --no-install-recommends \
    systemd systemd-sysv dbus sudo ca-certificates curl gnupg iproute2 \
    python3 rsync jq git procps \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable' > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /bin/bash tester \
    && usermod -aG docker tester \
    && echo 'tester ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/tester \
    && mkdir -p /etc/docker /srv/offhost \
    && chown tester:tester /srv/offhost \
    && systemctl enable docker
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
