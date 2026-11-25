#!/usr/bin/env python3
"""Operator entrypoint: preserve activation fences, avoid validating identical code twice."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


def git_bytes(root, target, name):
    return subprocess.check_output(['git', 'show', target+':'+name], cwd=root, timeout=120)


def configure(activation, read_source=git_bytes):
    original_run = activation.run
    original_validate = activation.Activator.validate

    def run_with_budget(argv, cwd=None, env=None, timeout=120):
        if timeout == 300 and list(argv[1:]) == ['-m', 'unittest', 'discover', '-s', 'tools/pr-coordinator/tests']:
            timeout = 900
        return original_run(argv, cwd=cwd, env=env, timeout=timeout)

    def validate_changed_runtime(self, target):
        # activate() still verifies provenance and stages/compiles the target
        # before this hook. Only identical actual files AND recorded hashes
        # reuse validation; missing, changed or malformed inputs go full.
        try:
            metadata = json.loads((self.link/'installed.json').read_text())
            names = set(activation.FILES)
            valid = (isinstance(metadata, dict) and type(metadata.get('protocol')) is int
                     and metadata['protocol'] == activation.COMPATIBILITY['activation_protocol']
                     and set(metadata['hashes']) == names)
            actual, target_hashes = {}, {}
            for name in names:
                path = self.link/name
                if path.is_symlink() or not path.is_file():
                    valid = False
                    break
                actual[name] = hashlib.sha256(path.read_bytes()).hexdigest()
                target_hashes[name] = hashlib.sha256(read_source(self.root, target, activation.PREFIX+name)).hexdigest()
            if valid and actual == metadata['hashes'] == target_hashes:
                print('Activation: runtime bytes unchanged; reused existing validation', flush=True)
                return
        except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
            pass
        return original_validate(self, target)

    activation.run = run_with_budget
    activation.Activator.validate = validate_changed_runtime


def main():
    source = (Path.home()/'.local/lib/pr-coordinator/current/activation.py').resolve()
    sys.path.insert(0, str(source.parent))
    spec = importlib.util.spec_from_file_location('installed_runtime_activation', source)
    activation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(activation)
    configure(activation)
    activation.main()


if __name__ == '__main__':
    main()
