"""SF-304: the controller setup commands in docs/ANSIBLE.md, offline.

Runs the documented first-run block in a shell whose PATH has no
ansible-playbook. The interpreter is a stand-in that builds a real virtual
environment (with its real activate script) but no pip; the stand-in pip
installs a recording ansible-playbook into that environment. Both playbook
invocations must resolve to the environment's copy, and the venv must be
created with the pinned CPython 3.12 named in requirements-controller.txt.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs/ANSIBLE.md"
REQS = REPO / "examples/ansible/requirements-controller.txt"


def run_block():
    text = DOC.read_text()
    blocks = re.findall(r"```sh\n(.*?)```", text, re.S)
    found = [b for b in blocks if "requirements-controller.txt" in b]
    if len(found) != 1:
        raise AssertionError(f"expected one controller setup block, got {len(found)}")
    return found[0]


class ControllerSetupDoc(unittest.TestCase):
    def test_requirements_pin_cpython_312_x86_64_linux(self):
        self.assertIn("CPython 3.12, x86_64 Linux", REQS.read_text())
        self.assertRegex(DOC.read_text(), r"CPython 3\.12 on x86_64 Linux")
        self.assertIn("python3.12 -m venv", run_block())

    def test_block_uses_the_pinned_venv_without_global_ansible(self):
        block = run_block()
        tmp = Path(tempfile.mkdtemp(prefix="sf304-ctl-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        home = tmp / "home"
        stubs = tmp / "stubs"
        checkout = tmp / "checkout"
        log = tmp / "calls.log"
        home.mkdir()
        stubs.mkdir()
        (checkout / "examples/ansible").mkdir(parents=True)
        shutil.copy(REQS, checkout / "examples/ansible/requirements-controller.txt")

        real_python = sys.executable
        (stubs / "python3.12").write_text(
            "#!/bin/sh\n"
            '[ "$1" = -m ] && [ "$2" = venv ] || exit 97\n'
            f'"{real_python}" -m venv --without-pip "$3" || exit 1\n'
            'cat > "$3/bin/pip" <<\'PIP\'\n'
            "#!/bin/sh\n"
            'dir=$(dirname "$0")\n'
            f'echo "pip $*" >> "{log}"\n'
            'printf \'%s\\n\' "#!/bin/sh" '
            f'"echo \\"ansible-playbook \\$0 \\$*\\" >> {log}" '
            '> "$dir/ansible-playbook"\n'
            'chmod +x "$dir/ansible-playbook"\n'
            "PIP\n"
            'chmod +x "$3/bin/pip"\n'
        )
        (stubs / "python3.12").chmod(0o755)

        tools = tmp / "tools"
        tools.mkdir()
        for tool in ("dirname", "cat", "chmod", "printf", "echo", "sh"):
            path = shutil.which(tool)
            if path:
                os.symlink(path, tools / tool)
        env = {
            "HOME": str(home),
            "PATH": f"{stubs}:{tools}",
        }
        self.assertIsNone(shutil.which("ansible-playbook", path=env["PATH"]))
        script = f"set -eu\ncd {checkout}\n{block}"
        proc = subprocess.run(
            ["/bin/sh", "-c", script], env=env, capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = log.read_text().splitlines()
        venv_bin = home / ".venvs/suifleet-ansible/bin"
        self.assertEqual(
            calls[0], "pip install --require-hashes -r requirements-controller.txt"
        )
        plays = [c for c in calls if c.startswith("ansible-playbook ")]
        self.assertEqual(len(plays), 2, calls)
        for call in plays:
            self.assertTrue(
                call.startswith(f"ansible-playbook {venv_bin}/ansible-playbook "),
                call,
            )
        self.assertIn("--check --diff", plays[0])


if __name__ == "__main__":
    unittest.main()
