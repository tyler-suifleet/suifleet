"""SF-304: the Ansible role's enroll.toml check (files/enroll_config.py), offline.

The CLI parses enroll.toml as TOML, so the check must accept and reject on
parsed values: noncanonical but equivalent spellings pass and are rebuilt
canonically, and spellings that hide or add a field are refused.

The script runs on the Ansible controller (ansible-core 2.20 needs Python
3.12+, which has tomllib). The CI runner's python3 may be older, so the script
and every TOML parse run under a Python 3.11+ interpreter: this one, one on
PATH, or one in the hosted runner's tool cache. Without one, the tests fail in
CI and are skipped elsewhere.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "examples/ansible/roles/suifleet/files/enroll_config.py"
sys.path.insert(0, str(ROOT / "scripts"))
import claims  # noqa: E402

POLICY = "0x" + "ab" * 32
CANONICAL = {
    "manifest_path": "deployment.json",
    "policy_id": POLICY,
    "claim_key_path": "claim.key",
    "name": "pump-7",
    "keypair_path": "/etc/suifleet/device.key",
}
PARSE = "import json, sys, tomllib; print(json.dumps(tomllib.loads(sys.stdin.read())))"


def modern_python():
    if sys.version_info >= (3, 11):
        return sys.executable
    candidates = [shutil.which(f"python3.{minor}") for minor in range(14, 10, -1)]
    candidates += sorted(glob.glob("/opt/hostedtoolcache/Python/3.1[1-9]*/x64/bin/python3"), reverse=True)
    return next((c for c in candidates if c), None)


PYTHON = modern_python()
if PYTHON is None and os.environ.get("CI"):
    raise RuntimeError("no Python 3.11+ (tomllib) for the enroll.toml check tests")


def toml(text):
    result = subprocess.run([PYTHON, "-I", "-c", PARSE], input=text, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def run(text):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "enroll.toml"
        path.write_text(text)
        return subprocess.run([PYTHON, "-I", str(SCRIPT), str(path)], capture_output=True, text=True)


def batch_file(alias="a"):
    return claims.enroll_toml({"alias": alias, "serial": "s"}, POLICY, "/etc/suifleet/device.key")


@unittest.skipIf(PYTHON is None, "no Python 3.11+ (tomllib) outside CI")
class EnrollConfigTest(unittest.TestCase):
    def accepted(self, text):
        result = run(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        rebuilt = json.loads(result.stdout)["toml"]
        self.assertEqual(toml(rebuilt), toml(text))
        return rebuilt

    def refused(self, text, reason):
        result = run(text)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn(reason, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_claims_batch_file_is_accepted(self):
        text = claims.enroll_toml({"alias": "pump-7", "serial": "SN1"}, POLICY, "/etc/suifleet/device.key")
        rebuilt = self.accepted(text)
        self.assertEqual(toml(rebuilt), CANONICAL)
        self.assertNotIn("#", rebuilt)

    def test_noncanonical_equivalent_spellings_are_rebuilt(self):
        text = "\n".join([
            "# comment",
            "'name' = 'pump-7'",
            '"keypair_path" = """/etc/suifleet/device.key"""',
            "claim_key_path = 'claim.key'  # trailing comment",
            'manifest_path = "deploy\\u006dent.json"',
            f'policy_id = "{POLICY}"',
            'arch = "aarch64"',
            '["managed"]',
            f'customer_id = "0x{"01" * 32}"',
            f'fleet_id = "0x{"02" * 32}"',
            f'budget_id = "0x{"03" * 32}"',
        ]) + "\n"
        rebuilt = self.accepted(text)
        self.assertIn('manifest_path = "deployment.json"\n', rebuilt)
        self.assertIn("[managed]\n", rebuilt)

    def test_line_hidden_in_a_multiline_string_does_not_count(self):
        # A line-oriented check sees the expected manifest_path line; TOML sees another value.
        text = "\n".join([
            'name = """',
            'manifest_path = "deployment.json"',
            '"""',
            'manifest_path = "other/deployment.json"',
            f'policy_id = "{POLICY}"',
            'claim_key_path = "claim.key"',
            'keypair_path = "/etc/suifleet/device.key"',
        ]) + "\n"
        self.refused(text, "manifest_path must be 'deployment.json'")

    def test_quoted_key_for_another_manifest_is_refused(self):
        text = batch_file()
        text = text.replace('manifest_path = "deployment.json"', '"manifest_path" = \'../other/deployment.json\'')
        self.refused(text, "manifest_path must be")

    def test_quoted_and_dotted_signer_tables_are_refused(self):
        base = batch_file()
        for spelling in ('["signer"]\nkind = "service"\n', "[ 'signer' ]\nkind = 'service'\n",
                         'signer.kind = "service"\n', 'signer = { kind = "service" }\n'):
            with self.subTest(spelling=spelling):
                text = base + spelling if spelling.startswith("[") else spelling + base
                self.refused(text, "[signer]")

    def test_other_key_and_unknown_fields_are_refused(self):
        base = batch_file()
        self.refused(base.replace("/etc/suifleet/device.key", "/etc/suifleet/other.key"), "keypair_path must be")
        self.refused(base.replace('claim_key_path = "claim.key"', 'claim_key_path = "/tmp/claim.key"'), "claim_key_path must be")
        self.refused(base + 'extra = "x"\n', "unexpected field(s): extra")
        self.refused(base.replace(f'"{POLICY}"', "42"), "policy_id must be an object ID")
        self.refused(base + '[managed]\ncustomer_id = "0x1"\n', "[managed] must hold exactly")

    def test_control_characters_and_invalid_toml_are_refused(self):
        base = batch_file()
        self.refused(base.replace('name = "a"', 'name = "a\\u007f"'), "name must be")
        self.refused(base.replace('name = "a"', 'name = "a\\nb"'), "name must be")
        self.refused(base + "manifest_path = 'x'\n", "enroll.toml refused")
        self.refused("not toml = = \n", "enroll.toml refused")

    def test_unicode_name_round_trips(self):
        base = batch_file("pumpe-ü-😀")
        self.assertEqual(toml(self.accepted(base))["name"], "pumpe-ü-😀")

    def policy_output(self, policy):
        result = run(batch_file().replace(f'"{POLICY}"', f'"{policy}"'))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["policy_id"]

    def test_policy_id_is_printed_in_canonical_form(self):
        # The role records this ID with each attempt and compares it with the
        # batch's before an on-chain lookup, so every spelling of the same
        # object ID yields one string, and different objects differ.
        self.assertEqual(self.policy_output(POLICY), POLICY)
        self.assertEqual(self.policy_output("0x" + "AB" * 32), POLICY)
        self.assertEqual(self.policy_output("0xAbC"), "0x" + "0" * 61 + "abc")
        self.assertEqual(self.policy_output("0x" + "0" * 61 + "abc"), "0x" + "0" * 61 + "abc")
        self.assertNotEqual(self.policy_output("0x" + "ab" * 31 + "ac"), POLICY)

if __name__ == "__main__":
    unittest.main()
