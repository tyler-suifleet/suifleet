# Every toolchain input is pinned so local builds and CI test the same versions.
# Bump these deliberately together with lockfiles and compatibility tests.
FROM ubuntu:22.04@sha256:5ec03bb3441e8b0bf3b4f9cd4629a1ae763010dc3035bb8da3ae6cf026486401

ARG SUI_VERSION=testnet-v1.81.1
ARG SUI_SHA256=d921910fdedf5ae2bc3f08a7236c57819a457ce53a8cc60c85f3653c7831b5d4
ARG NODE_MAJOR=22
ARG NODE_VERSION=22.22.2-1nodesource1
ARG PNPM_VERSION=11.0.8
ARG RUST_VERSION=1.94.0
# Chrome for Testing headless shell for the SF-010 wallet gate: the build
# Playwright 1.64.1 (dapp/package.json) drives, revision 1248.
ARG CHROME_VERSION=156.0.8078.4
ARG CHROME_SHA256=c3e50596aef52c58f8fda3c6b3411c33d34c88b177b3ed1c2243359fd3b8ba20
ARG USER_UID=1000
ARG USER_GID=1000

ENV DEBIAN_FRONTEND=noninteractive

# ── System packages + globally-accessible tools (as root) ────────────────────

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    build-essential \
    pkg-config \
    python3 \
    libssl-dev \
    ca-certificates \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# SUI CLI → /usr/local/bin (accessible to all users)
RUN curl -fL -o /tmp/sui.tgz \
    "https://github.com/MystenLabs/sui/releases/download/${SUI_VERSION}/sui-${SUI_VERSION}-ubuntu-x86_64.tgz" \
    && echo "${SUI_SHA256}  /tmp/sui.tgz" | sha256sum -c - \
    && tar -xzf /tmp/sui.tgz -C /usr/local/bin \
    && rm /tmp/sui.tgz \
    && sui --version

# Headless Chromium → /opt/chrome-headless-shell (libraries from its deb.deps)
RUN apt-get update && apt-get install -y --no-install-recommends unzip fonts-liberation \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcups2 libdbus-1-3 libexpat1 \
    libgbm1 libglib2.0-0 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 \
    libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
    && curl -fL -o /tmp/chrome.zip \
    "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chrome-headless-shell-linux64.zip" \
    && echo "${CHROME_SHA256}  /tmp/chrome.zip" | sha256sum -c - \
    && unzip -q /tmp/chrome.zip -d /opt && mv /opt/chrome-headless-shell-linux64 /opt/chrome-headless-shell \
    && rm /tmp/chrome.zip && rm -rf /var/lib/apt/lists/* \
    && /opt/chrome-headless-shell/chrome-headless-shell --version
ENV CHROME_EXECUTABLE=/opt/chrome-headless-shell/chrome-headless-shell

# Node.js + pnpm (global install, any user can run them)
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends "nodejs=${NODE_VERSION}" \
    && npm install -g "pnpm@${PNPM_VERSION}" \
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
    | sh -s -- -y --default-toolchain "${RUST_VERSION}" --profile minimal --component rust-src \
    && cargo --version

WORKDIR /workspace

CMD ["sleep", "infinity"]
