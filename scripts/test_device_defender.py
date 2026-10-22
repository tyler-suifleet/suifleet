#!/usr/bin/env python3
"""SF-062 real Linux daemon and shared AWS IoT adapter over mutual TLS.
Network-none container; disposable test certificates; no AWS account or Detect.
Build bins/examples first. Unit tests exercise deterministic procfs/sampling/time.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time
import test_aws_iot as harness
from device_defender_schema import validate_report


def main(args):
    harness.RUNNER = "suifleet-sf062-runner"
    sh, control, aws, wait = harness.sh, harness.control, harness.aws, harness.wait
    root = harness.ROOT
    checks = []
    work = Path(tempfile.mkdtemp(prefix="suifleet-sf062-"))
    (work / "pki").mkdir(mode=0o755)
    def check(text):
        checks.append(text)
        print("PASS:", text, flush=True)
    def state(label):
        return control("/defender-state", label=label)
    topic = "$aws/things/thing1/defender/metrics/json"
    def reports():
        return [json.loads(p[2]) for p in aws("snapshot")["published"] if p[1] == topic]
    def response(report, suffix="accepted", **override):
        body = dict(thingName="thing1", reportId=report["header"]["report_id"], status=suffix.upper())
        body.update(override)
        aws("publish", topic=f"{topic}/{suffix}", payload=body)
    try:
        sh("docker", "run", "-d", "--name", harness.RUNNER, "--network", "none", "--user", "0",
           "-v", f"{root}:/workspace:ro", "-v", f"{work}/pki:/pki",
           "-e", f"HOST_UID={os.getuid()}", "-e", f"HOST_GID={os.getgid()}", "-e", "PYTHONDONTWRITEBYTECODE=1",
           args.image, "python3", "/workspace/scripts/test_aws_iot.py", "--serve")
        wait(lambda: "READY" in sh("docker", "logs", harness.RUNNER, check=False), "runner did not start", 90)
        # Start disconnected: reports are collected locally with zero send attempts.
        aws("down", down=True)
        control("/start", phase="aws", defender="accept")
        wait(lambda: len(state("accept").get("queue", [])) == 1, "offline report not persisted")
        assert state("accept")["queue"][0]["attempts"] == 0
        control("/stop", kill=True)
        control("/start", phase="aws", defender="accept")
        assert state("accept")["queue"][0]["attempts"] == 0
        check("offline report persists across daemon SIGKILL; disconnected time consumes no retries")
        # No AWS policy grant yet: publish disconnect is a failure, never success.
        aws("allow", action="subscribe", pattern=f"{topic}/*", allow=True)
        aws("down", down=False)
        wait(lambda: any(p[1] == "publish" and p[2] == topic for p in aws("snapshot")["denied"]), "policy denial not exercised")
        assert state("accept")["accepted"] == 0 and state("accept")["queue"]
        check("thing policy denies Defender publication; queue remains unaccepted")
        aws("allow", action="publish", pattern=topic, allow=True)
        wait(lambda: reports(), "report did not reach TLS endpoint", 90)
        report = reports()[0]
        validate_report(report)
        assert report["header"]["version"] == "1.0" and isinstance(report["header"]["report_id"], int)
        metrics = report["metrics"]
        for kind in ("listening_tcp_ports", "listening_udp_ports"):
            assert metrics[kind]["total"] == len(metrics[kind]["ports"])
            assert all(isinstance(p["port"], int) and 0 < p["port"] <= 65535 for p in metrics[kind]["ports"])
        assert metrics["tcp_connections"]["established_connections"]["total"] >= 1
        assert "network_stats" not in metrics  # baseline, not lifetime counters
        assert state("accept")["accepted"] == 0, "PUBACK falsely counted as service acceptance"
        response(report, thingName="other")
        response(report, reportId=0)
        time.sleep(1)
        assert state("accept")["accepted"] == 0 and state("accept")["queue"]
        response(report)
        wait(lambda: state("accept").get("accepted") == 1, "correlated acceptance not recorded")
        assert not state("accept")["queue"]
        assert harness.ipc("publish", "telemetry/defender-probe", {"probe": 1})["ok"]
        denied = harness.ipc("publish", topic, report)
        assert not denied["ok"] and denied["error"] == "request_failed", denied
        check("real procfs JSON 1.0 and reserved topic traverse shared mTLS/ALPN connection; PUBACK/wrong thing/wrong id do not accept; correlated service response does")
        before = len(reports())
        control("/stop", kill=True)
        control("/start", phase="aws", defender="accept")
        time.sleep(3)
        assert len(reports()) == before and not state("accept")["queue"]
        check("accepted report not replayed or resampled on restart inside configured 300-second interval")
        control("/stop")
        control("/start", phase="aws", defender="reject")
        wait(lambda: len(reports()) > before, "rejection sample did not arrive")
        rejected = reports()[-1]
        response(rejected, "rejected", statusDetails={"ErrorCode":"InvalidPayload", "ErrorMessage":"fixture rejection"})
        wait(lambda: state("reject").get("rejected") == 1, "rejection not recorded")
        assert state("reject")["accepted"] == 0 and not state("reject")["queue"]
        check("correlated rejected response counted separately and removed without falsely accepting or retrying invalid payload")
        control("/stop")
        control("/start", phase="aws", defender="retry")
        wait(lambda: state("retry").get("queue") and state("retry")["queue"][0]["attempts"] >= 1, "retry sample missing")
        retry_report = state("retry")["queue"][0]["report"]
        # Endpoint PUBACKs but never provides a service outcome. Persisted
        # attempt budget is tested across a forced restart, then exhaustion.
        control("/stop", kill=True)
        control("/start", phase="aws", defender="retry")
        wait(lambda: state("retry").get("dropped") == 1, "retry allowance did not exhaust", 180)
        assert state("retry")["accepted"] == 0 and not state("retry")["queue"]
        retried = [report for report in reports() if report["header"]["report_id"] == retry_report["header"]["report_id"]]
        assert 1 <= len(retried) <= 3 and all(report == retry_report for report in retried)
        check("missing service outcomes retry identical reports after restart, then exhaust the configured persistent allowance; never counted accepted")
        try:
            control("/scan")
            raise RuntimeError("scanned a running daemon's changing state")
        except AssertionError as error:
            assert "scan requires a stopped daemon" in str(error), error
        control("/stop")
        assert not control("/scan")["found"]
        check("no private key material in daemon logs or persisted state")
        if args.evidence:
            Path(args.evidence).write_text(json.dumps({"ticket":"SF-062", "adapter":"device-defender-metrics", "reference_version":"4.0.0",
                "schema":"1.0", "environment":"network-none container; real daemon/procfs/shared TLS MQTT adapter; AWS-shaped endpoint",
                "checks":checks,"unrun":["Live AWS account/policy/Device Defender Detect (unauthorized, no credentials)",
                    "Physical Pi acceptance (#32)","Actual mainnet acceptance (#159)"]}, indent=2) + "\n")
    except Exception:
        print(sh("docker", "logs", "--tail", "30", harness.RUNNER, check=False))
        try:
            print(control("/log")["log"])
        except Exception:
            pass
        raise
    finally:
        sh("docker", "rm", "-f", harness.RUNNER, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="suifleet-dev:sf037")
    parser.add_argument("--evidence")
    main(parser.parse_args())
