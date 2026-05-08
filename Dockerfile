FROM ubuntu:22.04

ARG SUI_VERSION=testnet-v1.71.1
ARG NODE_MAJOR=22
ARG USER_UID=1000
ARG USER_GID=1000

ENV DEBIAN_FRONTEND=noninteractive

# ── System packages + globally-accessible tools (as root) ────────────────────

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    build-essential \
    pkg-config \
    libssl-dev \
    ca-certificates \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# SUI CLI → /usr/local/bin (accessible to all users)
RUN curl -fL \
    "https://github.com/MystenLabs/sui/releases/download/${SUI_VERSION}/sui-${SUI_VERSION}-ubuntu-x86_64.tgz" \
    | tar -xz -C /usr/local/bin \
    && sui --version

# Node.js + pnpm (global install, any user can run them)
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g pnpm \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && pnpm --version

# ── Create non-root user matching host UID/GID ───────────────────────────────

RUN groupadd --gid ${USER_GID} dev \
    && useradd --uid ${USER_UID} --gid ${USER_GID} -m --shell /bin/bash dev

# Pre-create volume mount points with dev ownership so Docker initialises
# named volumes from these directories rather than creating them as root.
RUN mkdir -p /home/dev/.sui /home/dev/.move \
              /home/dev/.cargo/registry /home/dev/.cargo/git \
              /home/dev/.local/share/pnpm/store \
    && chown -R ${USER_UID}:${USER_GID} /home/dev

USER dev

ENV CARGO_HOME=/home/dev/.cargo \
    RUSTUP_HOME=/home/dev/.rustup \
    PATH="/home/dev/.cargo/bin:${PATH}" \
    HOME=/home/dev \
    INSIDE_DEV_CONTAINER=1

# Rust toolchain (installed into the dev user's home)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --default-toolchain 1.87.0 --profile minimal --component rust-src \
    && cargo --version

WORKDIR /workspace

CMD ["sleep", "infinity"]
