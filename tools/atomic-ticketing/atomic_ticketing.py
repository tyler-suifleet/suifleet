#!/usr/bin/env python3
"""Portable atomic tickets across repositories on one WSL host."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ticket_common import *
from ticket_github import *
from ticket_core import *
from ticket_cli import *

if __name__ == '__main__':
    try:
        main()
    except (CoordinationError, ValueError, RuntimeError, sqlite3.Error, OSError, KeyError) as error:
        raise SystemExit('Coordination stopped: ' + str(error) + '\nAny acquired claim remains reserved; inspect list/show before retrying.')
