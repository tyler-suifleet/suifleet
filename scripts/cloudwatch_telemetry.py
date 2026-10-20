#!/usr/bin/env python3
"""Explicitly export a trusted SF-019 schema-1 snapshot to the metrics socket.

The source must be an operator-provided local snapshot or a verified indexer
export. This maps selected numeric values; it does not authenticate signatures.
"""
import argparse
import json
import socket


def messages(snapshot, namespace):
    if type(snapshot.get("schema")) is not int or snapshot["schema"] != 1:
        raise ValueError("unsupported telemetry schema")
    def counter(value):
        if type(value) is not int or not 0 <= value <= 2**64-1:
            raise ValueError("invalid telemetry counter")
        return value
    observed = counter(snapshot["observed_ms"])
    started = counter(snapshot["started_ms"])
    if observed < started or snapshot["health"] not in ("healthy", "degraded"):
        raise ValueError("invalid telemetry health/time")
    components = snapshot["components"]
    if not isinstance(components, list) or len(components) > 64:
        raise ValueError("invalid telemetry components")
    values = [("Healthy", int(snapshot["health"] == "healthy"), "Count", []),
              ("Uptime", (observed-started)/1000, "Seconds", []),
              ("TelemetryDropped", counter(snapshot["dropped"]), "Count", []),
              ("PendingReceipts", counter(snapshot["pending_receipts"]), "Count", []),
              ("SyncFailures", counter(snapshot["sync"]["consecutive_failures"]), "Count", [])]
    for component in components:
        name = component["name"]
        if not isinstance(name, str) or not 0 < len(name) <= 128 or type(component["installed"]) is not bool:
            raise ValueError("invalid telemetry component")
        dimensions = [dict(name="component", value=name)]
        values.append(("Installed", int(component["installed"]), "Count", dimensions))
        if component["restarts"] is not None:
            values.append(("Restarts", counter(component["restarts"]), "Count", dimensions))
    return [dict(request=dict(namespace=namespace, metricData=dict(metricName=name, value=value,
                 unit=unit, dimensions=dimensions, timestamp=observed/1000))) for name,value,unit,dimensions in values]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--socket", required=True)
    args = parser.parse_args()
    with open(args.snapshot, "rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError("snapshot too large")
    records = messages(json.loads(raw), args.namespace)
    for record in records:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(args.socket)
            connection.sendall(json.dumps(record).encode()+b"\n")
            with connection.makefile("rb") as source:
                raw = source.readline(16385)
            if len(raw) > 16384 or not raw.endswith(b"\n") or json.loads(raw).get("status") != "success":
                raise ValueError("metric not accepted")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("telemetry metrics export failed") from None
