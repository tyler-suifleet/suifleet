"""SF-304: each rollout stages its artifact at a path named by its pinned digest, offline.

Overlapping rollouts of different releases must never share the file one of
them checked and then installs (scripts/test_ansible.py runs that overlap on
real hosts). This checks the role source: the staging path names the pinned
SHA-256, and the stage, the host-side check and the install all use it.
"""
import re
import unittest
from pathlib import Path

TASKS = Path(__file__).resolve().parents[2] / "examples/ansible/roles/suifleet/tasks"
PROFILES = {
    # file: (staging variable, pinned digest variable, install task, install source key)
    "install_binary.yml": ("suifleet_binary_staging", "suifleet_binary_sha256", "Install the verified binary", "src"),
    "install_deb.yml": ("suifleet_deb_staging", "suifleet_deb_sha256", "Install or upgrade the package", "deb"),
}


def task(text, name):
    block = text.split(f"- name: {name}\n", 1)[1]
    return block.split("\n- name: ", 1)[0]


class StagingPath(unittest.TestCase):
    def test_staging_path_names_the_pinned_digest(self):
        for file, (var, digest, install, key) in PROFILES.items():
            with self.subTest(file=file):
                text = (TASKS / file).read_text()
                chosen = re.findall(rf"^    {var}: \"(.*)\"$", text, re.M)
                self.assertEqual(len(chosen), 1, "one definition of the staging path")
                self.assertTrue(chosen[0].startswith("/var/cache/suifleet/"))
                self.assertIn("{{ %s }}" % digest, chosen[0])
                ref = '"{{ %s }}"' % var
                self.assertIn(f"    dest: {ref}\n", task(text, "Stage the authenticated " + ("binary" if "binary" in file else "package")))
                self.assertIn(f"    path: {ref}\n", task(text, "Check the staged " + ("binary" if "binary" in file else "package") + " on the host"))
                self.assertIn(f"    {key}: {ref}\n", task(text, install))
                # No other literal path into the staging directory.
                literal = [line for line in text.splitlines() if "/var/cache/suifleet/" in line]
                self.assertEqual(literal, [f'    {var}: "{chosen[0]}"'])
                # Other releases' staged files are pruned only after the install, keeping this one.
                self.assertLess(text.index(f"- name: {install}\n"), text.index("prune_staging.yml"))
                self.assertIn(f'suifleet_staging_keep: {ref}', text)


if __name__ == "__main__":
    unittest.main()
