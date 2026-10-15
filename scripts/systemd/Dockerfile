# Disposable systemd host for scripts/test_supervisor_systemd.py: real systemd
# (PID 1) on the host's cgroup v2, running the shipped suifleet units.
FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
RUN apt-get update \
 && apt-get install -y --no-install-recommends systemd systemd-sysv python3 procps \
 && rm -rf /var/lib/apt/lists/* \
 && systemctl mask systemd-logind.service getty.target console-getty.service systemd-udevd.service
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
