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
    result = subprocess.run(command,input=(directory/'prompt.txt').read_text(),text=True,stdout=log,stderr=subprocess.STDOUT)
atomic(directory/'exit.json',{'returncode':result.returncode})
sys.exit(result.returncode)
