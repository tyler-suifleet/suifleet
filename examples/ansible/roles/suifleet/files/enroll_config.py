"""Validates a batch folder's enroll.toml and prints the configuration the role stages.

Usage (on the controller): python3 -I enroll_config.py ENROLL_TOML

The suifleet CLI parses enroll.toml as TOML (EnrollConfig in
suifleet-edge/src/config.rs), so the role checks the parsed values, not the
file's spelling: a multiline string, a quoted key or a quoted table name can
hide or add a field that a line match would miss. The effective manifest,
claim and key paths must be the batch-relative deployment.json and claim.key
and the device's own /etc/suifleet/device.key, with no [signer] and no other
field. On success, prints JSON {"toml": TEXT}: a canonical enroll.toml built
only from the validated values, which the role stages instead of the batch's
file. It parses back to the same values. Exit 2 with a reason on stderr
otherwise. The file holds only public values (no keys).
"""
import json
import re
import sys
import tomllib

FIXED = {
    "manifest_path": "deployment.json",
    "claim_key_path": "claim.key",
    "keypair_path": "/etc/suifleet/device.key",
}
OPTIONAL = ("arch", "managed")
MANAGED = ("customer_id", "fleet_id", "budget_id")
OBJECT_ID = re.compile(r"0x[0-9a-fA-F]{1,64}")
# Printable text without control characters (TOML forbids most raw, JSON
# escapes the rest with sequences TOML would read differently, e.g. DEL).
TEXT = re.compile(r"[^\x00-\x1f\x7f]{1,256}")


class Invalid(Exception):
    pass


def quote(value):
    # A JSON string of control-free text is a TOML basic string with the same value.
    return json.dumps(value, ensure_ascii=False)


def validate(data):
    """Returns the canonical TOML text for parsed enroll.toml values, or raises Invalid."""
    if "signer" in data:
        raise Invalid("it has a [signer] table, which would sign with another key")
    unknown = sorted(set(data) - set(FIXED) - {"policy_id", "name"} - set(OPTIONAL))
    if unknown:
        raise Invalid(f"unexpected field(s): {', '.join(unknown)}")
    for field, expected in FIXED.items():
        if data.get(field) != expected:
            raise Invalid(f"{field} must be {expected!r}, not {data.get(field)!r}")
    policy = data.get("policy_id")
    if not isinstance(policy, str) or not OBJECT_ID.fullmatch(policy):
        raise Invalid("policy_id must be an object ID")
    name = data.get("name")
    if not isinstance(name, str) or not TEXT.fullmatch(name):
        raise Invalid("name must be 1 to 256 printable characters")
    lines = [
        f"manifest_path = {quote(FIXED['manifest_path'])}",
        f"policy_id = {quote(policy)}",
        f"claim_key_path = {quote(FIXED['claim_key_path'])}",
        f"name = {quote(name)}",
        f"keypair_path = {quote(FIXED['keypair_path'])}",
    ]
    if "arch" in data:
        arch = data["arch"]
        if not isinstance(arch, str) or not TEXT.fullmatch(arch):
            raise Invalid("arch must be printable text")
        lines.append(f"arch = {quote(arch)}")
    if "managed" in data:
        managed = data["managed"]
        if not isinstance(managed, dict) or set(managed) != set(MANAGED):
            raise Invalid(f"[managed] must hold exactly {', '.join(MANAGED)}")
        lines.append("")
        lines.append("[managed]")
        for field in MANAGED:
            value = managed[field]
            if not isinstance(value, str) or not OBJECT_ID.fullmatch(value):
                raise Invalid(f"managed.{field} must be an object ID")
            lines.append(f"{field} = {quote(value)}")
    text = "\n".join(lines) + "\n"
    if tomllib.loads(text) != data:
        raise Invalid("it does not round-trip to the same values")
    return text


def main(argv):
    if len(argv) != 2:
        print("usage: enroll_config.py ENROLL_TOML", file=sys.stderr)
        return 2
    try:
        with open(argv[1], "rb") as handle:
            data = tomllib.load(handle)
        text = validate(data)
    except (OSError, tomllib.TOMLDecodeError, Invalid) as error:
        print(f"enroll.toml refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"toml": text}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
