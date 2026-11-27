"""SF-304: the digest-named staging path only ever holds verified bytes, offline.

Overlapping rollouts (same or different pins, or a controller artifact that
changes after its preflight check) must never install bytes no run verified
(scripts/test_ansible.py runs those overlaps on real hosts). This checks the
role source: each run uploads to its own private file, checks that file's
SHA-256 on the host, and only then renames it to the path named by the pinned
digest, from which it installs; a file already at that path is reused only
after the same check.
"""
import re
import unittest
from pathlib import Path

TASKS = Path(__file__).resolve().parents[2] / "examples/ansible/roles/suifleet/tasks"
PROFILES = {
    # file: (label, staging var, incoming var, pin var, cached register, install task, install source key)
    "install_binary.yml": ("binary", "suifleet_binary_staging", "suifleet_binary_incoming", "suifleet_binary_sha256",
                           "suifleet_binary_cached", "Install the verified binary", "src"),
    "install_deb.yml": ("package", "suifleet_deb_staging", "suifleet_deb_incoming", "suifleet_deb_sha256",
                        "suifleet_deb_cached", "Install or upgrade the package", "deb"),
}


def task(text, name, indent=""):
    """The source of the task with this name, up to the next task at the same indent."""
    head = f"\n{indent}- name: {name}\n"
    assert text.count(head) == 1, name
    block = text.split(head, 1)[1]
    return block.split(f"\n{indent}- name: ", 1)[0]


class Staging(unittest.TestCase):
    def test_private_upload_verified_before_publication(self):
        for file, (label, staging, incoming, pin, cached, install, key) in PROFILES.items():
            with self.subTest(file=file):
                text = (TASKS / file).read_text()
                ref = '"{{ %s }}"' % staging
                inc = '"{{ %s }}"' % incoming

                chosen = re.findall(rf"^    {staging}: \"(.*)\"$", text, re.M)
                self.assertEqual(len(chosen), 1, "one definition of the staging path")
                self.assertTrue(chosen[0].startswith("/var/cache/suifleet/"))
                self.assertIn("{{ %s }}" % pin, chosen[0])

                check = task(text, f"Check for an already verified staged {label}")
                self.assertIn(f"    path: {ref}\n", check)
                self.assertIn("    checksum_algorithm: sha256\n", check)
                self.assertIn(f"  register: {cached}\n", check)
                self.assertIn("  check_mode: false", check)

                block = task(text, f"Upload, verify and publish the {label}")
                # The upload is skipped only when the cached file has the pinned digest.
                self.assertIn(f"  when: not ({cached}.stat.exists and {cached}.stat.checksum == {pin})\n", block)
                steps = re.findall(r"^    - name: (.*)$", block.split("\n  rescue:\n", 1)[0], re.M)
                self.assertEqual(steps, ["Choose this run's private upload path", f"Stage the authenticated {label}",
                                         f"Check the staged {label} on the host",
                                         f"Refuse a staged {label} that differs from the pinned SHA-256",
                                         f"Publish the verified {label} under its digest-named path"])
                upload = task(block, "Choose this run's private upload path", "    ")
                self.assertIn("/var/cache/suifleet/incoming-{{ %s }}-" % pin, upload)
                self.assertIn("lookup('ansible.builtin.password', '/dev/null',", upload)  # unique per run
                self.assertIn(".part\n", upload + "\n")
                self.assertIn(f"        dest: {inc}\n", task(block, f"Stage the authenticated {label}", "    "))
                self.assertIn(f"        path: {inc}\n", task(block, f"Check the staged {label} on the host", "    "))
                remote = re.search(r"register: (\w+)", task(block, f"Check the staged {label} on the host", "    ")).group(1)
                self.assertIn(f"          - {remote}.stat.checksum == {pin}\n",
                              task(block, f"Refuse a staged {label} that differs from the pinned SHA-256", "    "))
                self.assertIn(f"        argv: [mv, -f, --, {inc}, {ref}]\n",
                              task(block, f"Publish the verified {label} under its digest-named path", "    "))
                # A failure removes this run's upload and stops.
                rescue = block.split("\n  rescue:\n", 1)[1]
                self.assertIn(f"        path: {inc}\n        state: absent\n", rescue)
                self.assertIn("ansible.builtin.fail:", rescue)

                # The install reads only the digest-named path.
                self.assertIn(f"    {key}: {ref}\n", task(text, install))
                # The staging directory is named only by the two path definitions.
                literal = [line for line in text.splitlines() if "/var/cache/suifleet/" in line]
                self.assertEqual(len(literal), 2, literal)
                # Nothing but the verified rename writes the digest-named path.
                self.assertNotIn(f"dest: {ref}", text)
                # Other releases' staged files are pruned only after the install, keeping this one.
                self.assertLess(text.index(f"- name: {install}\n"), text.index("prune_staging.yml"))
                self.assertIn(f"suifleet_staging_keep: {ref}", text)

    def test_prune_keeps_in_flight_uploads(self):
        text = (TASKS / "prune_staging.yml").read_text()
        published = task(text, "Find staged artifacts of other releases")
        self.assertNotIn("incoming-", published)
        abandoned = task(text, "Find abandoned private uploads")
        self.assertIn('    patterns: ["incoming-*.part"]\n', abandoned)
        self.assertIn("    age: 1d\n", abandoned)


if __name__ == "__main__":
    unittest.main()
