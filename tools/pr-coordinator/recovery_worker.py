#!/usr/bin/env python3
"""Bound one recovery session and preserve process/exit evidence across restarts."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from recovery_watchdog import atomic, identity, read


def stop_group(child):
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # Kill surviving members even if their immediate parent already exited.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def main():
    directory = Path(sys.argv[1])
    atomic(directory/'process.json', identity(os.getpid()))
    child = None
    code = 125
    def interrupted(number, frame):
        raise InterruptedError('supervisor terminated')
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, interrupted)
    try:
        with (directory/'worker.log').open('a') as log:
            child = subprocess.Popen(read(directory/'command.json'), stdin=subprocess.PIPE,
                                     stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            atomic(directory/'child.json', dict(pid=child.pid))
            try:
                child.communicate((directory/'prompt.txt').read_text(),
                                  timeout=read(directory/'limits.json')['timeout_seconds'])
                code = child.returncode
            except subprocess.TimeoutExpired:
                code = 124
                stop_group(child)
    except InterruptedError:
        code = 143
        if child:
            stop_group(child)
    finally:
        atomic(directory/'exit.json', dict(returncode=code))
    return code


if __name__ == '__main__':
    sys.exit(main())
