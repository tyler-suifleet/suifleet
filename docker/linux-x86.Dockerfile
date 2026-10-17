# 32-bit Linux x86 software runner (SF-076, docs/LINUX_X86.md). A genuine
# i386 Debian userland: its loader, glibc, shell and Python are all 32-bit, and
# it has no 64-bit loader, so dynamically linked x86_64 programs cannot start in
# it. It runs on the host kernel's IA-32 compatibility layer; it is not a 32-bit
# kernel. The daemon is cross-built in docker/release.Dockerfile, not here.
#
# Pinned: base image digest (glibc 2.36) and the Python that drives the
# simulator tests.
FROM i386/debian:bookworm-slim@sha256:20b0f8a8485acad7597ecd7c1c46a342706ad159a6f3aae7673ede19af6d9228

ARG USER_UID=1000
ARG USER_GID=1000

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3=3.11.2-1+b1 \
    python3-minimal=3.11.2-1+b1 \
    && rm -rf /var/lib/apt/lists/* \
    && test "$(dpkg --print-architecture)" = i386 \
    && test ! -e /lib64/ld-linux-x86-64.so.2

RUN groupadd --gid ${USER_GID} runner \
    && useradd --uid ${USER_UID} --gid ${USER_GID} -m --shell /bin/sh runner

# The IPC suite alone runs as root (docker run --user 0:0) to create distinct
# peer UIDs; everything else runs unprivileged.
USER runner
WORKDIR /workspace
