#!/usr/bin/env python3
"""Compatibility entry point for the shared, multi-project ticket CLI."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/atomic-ticketing'))
from atomic_ticketing import *  # Re-export the library for existing regression tests.
ROOT = Path(__file__).resolve().parents[1]

if __name__ == '__main__':
    try:
        if '--root' not in sys.argv and '--project' not in sys.argv:
            sys.argv[1:1] = ['--root', str(ROOT)]
        main()
    except (CoordinationError, ValueError, RuntimeError, sqlite3.Error, OSError, KeyError) as error:
        raise SystemExit('Coordination stopped: ' + str(error) + '\nAny acquired claim remains reserved; inspect list/show before retrying.')
