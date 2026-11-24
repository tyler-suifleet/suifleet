"""Report whether running SuiFleet services still use replaced inputs (SF-304).

Usage: running_inputs.py UNIT=PATH[,PATH...] [UNIT=...]

For each unit, prints whether its main process predates the inputs on disk:
its executable was replaced (deleted inode), systemd has unloaded unit
changes, or a listed file or the unit's own files changed after the process
started. Read-only; prints one JSON object. An interrupted rollout that
installed new files but never restarted is detected on the next run.
"""
import datetime
import json
import os
import subprocess
import sys

# Kernel file timestamps are coarse; allow for that rounding.
TOLERANCE = 1.0


def show(unit):
    # systemd records the start time from the realtime clock when the process
    # starts, like a file's mtime when it is written, so later clock steps
    # affect neither.
    out = subprocess.run(["systemctl", "show", unit, "--timestamp=us+utc", "-p", "MainPID", "-p", "NeedDaemonReload",
                          "-p", "FragmentPath", "-p", "DropInPaths", "-p", "ExecMainStartTimestamp"],
                         check=True, capture_output=True, text=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def started(props):
    # "Tue 2026-11-24 21:35:24.123456 UTC"
    stamp = props.get("ExecMainStartTimestamp", "").split(" ", 1)[-1]
    moment = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f UTC")
    return moment.replace(tzinfo=datetime.timezone.utc).timestamp()


def check(unit, inputs):
    props = show(unit)
    pid = int(props.get("MainPID") or 0)
    if pid == 0:
        return dict(running=False, stale=False, reason="not running")
    if props.get("NeedDaemonReload") == "yes":
        return dict(running=True, stale=True, reason="unit files changed and are not loaded")
    exe = os.readlink(f"/proc/{pid}/exe")
    if exe.endswith(" (deleted)"):
        return dict(running=True, stale=True, reason="its executable was replaced")
    start = started(props)
    paths = [*inputs, props.get("FragmentPath", ""), *props.get("DropInPaths", "").split()]
    for path in filter(None, paths):
        try:
            modified = os.stat(path).st_mtime
        except FileNotFoundError:
            continue
        if modified > start + TOLERANCE:
            return dict(running=True, stale=True, reason=f"{path} changed after it started")
    return dict(running=True, stale=False, reason="current")


def main():
    report = {}
    for argument in sys.argv[1:]:
        unit, _, files = argument.partition("=")
        report[unit] = check(unit, [f for f in files.split(",") if f])
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
