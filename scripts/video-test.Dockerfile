# Test-only independent H.264/Matroska decoder; never shipped in the daemon.
FROM ubuntu:22.04@sha256:5ec03bb3441e8b0bf3b4f9cd4629a1ae763010dc3035bb8da3ae6cf026486401
# Refresh signed indexes before retrying downloads if the mirror changes.
RUN set -e; \
    set -- python3 ffmpeg=7:4.4.2-0ubuntu0.22.04.1; \
    for attempt in 1 2 3; do \
        if apt-get -o APT::Update::Error-Mode=any update && \
           apt-get install -y --no-install-recommends --download-only "$@"; then break; fi; \
        if [ "$attempt" -eq 3 ]; then exit 1; fi; \
        sleep 2; \
    done; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends --no-download "$@"; \
    rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
USER 65532:65532
