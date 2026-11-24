"""Report whether active SuiFleet units still use replaced inputs (SF-304).

Usage: running_inputs.py [--since] APPLIED UNIT=PATH[,PATH...] [UNIT=...]

APPLIED is the root-owned JSON record ({unit: digest}) that the role writes
only after the units were restarted onto, or verified against, those inputs.
For each unit this prints a SHA-256 digest over its listed input files and its
own unit files (fragment and drop-ins), and whether the active unit is stale:
the digest differs from the applied record (or there is none), systemd has
unloaded unit changes, or its main process runs a replaced executable. A
rollout interrupted after its files landed but before the restarts is
therefore detected on the next run, however quickly the files changed. No
timestamps are compared. Read-only; prints one JSON object.

With --since, an earlier report of the same units is read from stdin and each
unit also reports "changed_since" (its digest differs from that report) and
"not_restarted" (it was active and stale then and still runs the same
invocation), which the role's verification requires to be false.
"""
import hashlib
import json
import os
import subprocess
import sys


def show(unit):
    out = subprocess.run(["systemctl", "show", unit, "-p", "ActiveState", "-p", "MainPID", "-p", "InvocationID",
                          "-p", "NeedDaemonReload", "-p", "FragmentPath", "-p", "DropInPaths"],
                         check=True, capture_output=True, text=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def digest(paths):
    hasher = hashlib.sha256()
    for path in sorted(set(filter(None, paths))):
        hasher.update(path.encode() + b"\0")
        try:
            with open(path, "rb") as handle:
                hasher.update(hashlib.sha256(handle.read()).digest())
        except FileNotFoundError:
            hasher.update(b"missing")
    return hasher.hexdigest()


def check(unit, inputs, applied):
    props = show(unit)
    pid = int(props.get("MainPID") or 0)
    # A service is current only while its main process runs; a socket or path
    # unit while it is active (it holds the socket or watch it was started with).
    active = pid != 0 if unit.endswith(".service") else props.get("ActiveState") in ("active", "reloading")
    report = dict(active=active, invocation=props.get("InvocationID", ""),
                  digest=digest([*inputs, props.get("FragmentPath", ""), *props.get("DropInPaths", "").split()]),
                  applied=applied.get(unit), stale=False, reason="current",
                  reload_needed=props.get("NeedDaemonReload") == "yes",
                  exe_replaced=bool(pid) and os.readlink(f"/proc/{pid}/exe").endswith(" (deleted)"))
    if not active:
        report["reason"] = "not active"
    elif report["reload_needed"]:
        report.update(stale=True, reason="unit files changed and are not loaded")
    elif report["exe_replaced"]:
        report.update(stale=True, reason="its executable was replaced")
    elif report["applied"] is None:
        report.update(stale=True, reason="no record that it was started on the installed inputs")
    elif report["applied"] != report["digest"]:
        report.update(stale=True, reason="its inputs changed since they were applied")
    return report


def main():
    arguments = sys.argv[1:]
    since = arguments[:1] == ["--since"]
    if since:
        arguments = arguments[1:]
        previous = json.load(sys.stdin)
    try:
        with open(arguments[0]) as handle:
            applied = json.load(handle)
    except FileNotFoundError:
        applied = {}
    report = {}
    for argument in arguments[1:]:
        unit, _, files = argument.partition("=")
        report[unit] = current = check(unit, [f for f in files.split(",") if f], applied)
        if since:
            before = previous[unit]
            current["changed_since"] = before["digest"] != current["digest"]
            current["not_restarted"] = (before["stale"] and before["active"] and current["active"]
                                        and before["invocation"] == current["invocation"])
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
