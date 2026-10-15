# Release builder for SuiFleet daemon artifacts (SF-039). Kept separate from
# the development image: it contains only what determines the release bytes.
#
# Pinned: base image digest, Rust toolchain, rustup-init checksum, and the
# packages that shape the output (cross compiler, binutils, target libc).
# Utility packages (curl, git, …) follow Ubuntu security updates on purpose;
# they do not affect the artifact bytes. Bump pins deliberately, and record
# the new versions in docs/RELEASES.md.
FROM ubuntu:22.04@sha256:5ec03bb3441e8b0bf3b4f9cd4629a1ae763010dc3035bb8da3ae6cf026486401

ARG RUST_VERSION=1.94.0
ARG RUSTUP_VERSION=1.29.1
ARG RUSTUP_SHA256=dda7234360b7f578ca8b0ddcb80145646fa61a67c1720a5abc7051b35c9fcb71
ARG USER_UID=1000
ARG USER_GID=1000

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    python3 \
    xz-utils \
    gcc \
    libc6-dev \
    gcc-aarch64-linux-gnu=4:11.2.0-1ubuntu1 \
    gcc-11-aarch64-linux-gnu=11.4.0-1ubuntu1~22.04.3cross1 \
    binutils-aarch64-linux-gnu=2.38-4ubuntu2.12 \
    libc6-dev-arm64-cross=2.35-0ubuntu1cross3 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid ${USER_GID} builder \
    && useradd --uid ${USER_UID} --gid ${USER_GID} -m --shell /bin/bash builder \
    && mkdir -p /home/builder/.cargo/registry /home/builder/.cargo/git \
    && chown -R ${USER_UID}:${USER_GID} /home/builder

USER builder
ENV CARGO_HOME=/home/builder/.cargo \
    RUSTUP_HOME=/home/builder/.rustup \
    PATH=/home/builder/.cargo/bin:${PATH} \
    CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
    CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc \
    AR_aarch64_unknown_linux_gnu=aarch64-linux-gnu-ar

RUN curl -fsSL -o /tmp/rustup-init \
      "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/x86_64-unknown-linux-gnu/rustup-init" \
    && echo "${RUSTUP_SHA256}  /tmp/rustup-init" | sha256sum -c - \
    && chmod +x /tmp/rustup-init \
    && /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain "${RUST_VERSION}" \
         --target aarch64-unknown-linux-gnu \
    && rm /tmp/rustup-init \
    && rustc --version && aarch64-linux-gnu-gcc --version | head -1

WORKDIR /workspace
