#!/usr/bin/env python3
"""Persist actual CLI exit status so the daemon can recover across restarts."""
import json
import os
from pathlib import Path
import subprocess
import sys


def atomic(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    os.replace(temporary,path)


directory = Path(sys.argv[1])
identity = Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')',1)[1].split()[19]
atomic(directory/'process.json',{'pid':os.getpid(),'started':identity})
command = json.loads((directory/'command.json').read_text())
with open(directory/'worker.log','a') as log:
    limits = directory/'limits.json'
    timeout = json.loads(limits.read_text())['timeout_seconds'] if limits.exists() else None
    try:
        result = subprocess.run(command,input=(directory/'prompt.txt').read_text(),text=True,
                                stdout=log,stderr=subprocess.STDOUT,timeout=timeout)
        code = result.returncode
    except subprocess.TimeoutExpired:
        code = 124  # Descendants still fence reconciliation through the session scan.
atomic(directory/'exit.json',{'returncode':code})
sys.exit(code)
