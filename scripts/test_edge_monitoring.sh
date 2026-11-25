#!/usr/bin/env bash
# SF-303: customer-owned Prometheus/OpenTelemetry monitoring, end to end.
# Online only to fetch the pinned upstream server images; every daemon,
# adapter, Prometheus and collector process then runs network-none.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${SUIFLEET_DEV_IMAGE:?set pinned built dev image}"
# Multi-architecture index digests (Prometheus v3.5.0 LTS, OpenTelemetry Collector core image 0.140.0, binary 0.140.1).
PROMETHEUS_IMAGE=docker.io/prom/prometheus@sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996
OTELCOL_IMAGE=docker.io/otel/opentelemetry-collector@sha256:6852803128c97a37fd2bafb989a04a10e3af920737d8eee998eefdfcebc698be
out=.ci-cache/monitoring-bin
mkdir -p "$out"
extract() {
  local image=$1 source=$2 target=$3 container
  docker pull -q "$image" >/dev/null
  container=$(docker create "$image")
  docker cp "$container:$source" "$out/$target"
  docker rm "$container" >/dev/null
  chmod 0755 "$out/$target"
}
extract "$PROMETHEUS_IMAGE" /bin/prometheus prometheus
extract "$OTELCOL_IMAGE" /otelcol otelcol
docker run --rm --user 0:0 -v "$PWD:/workspace" -w /workspace "$SUIFLEET_DEV_IMAGE" \
  cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples -j 2
docker run --rm --network none --user 0:0 -e PYTHONDONTWRITEBYTECODE=1 -e SF303_EVIDENCE \
  -v "$PWD:/workspace" -w /workspace "$SUIFLEET_DEV_IMAGE" \
  sh -ec 'python3 -m unittest discover -s scripts/tests -p test_edge_monitoring.py -v
          python3 scripts/test_edge_monitoring.py --bin-dir .ci-cache/monitoring-bin ${SF303_EVIDENCE:+--evidence "$SF303_EVIDENCE"}'
