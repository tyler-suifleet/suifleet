#!/usr/bin/env python3
"""Bulk claim keys for SF-021/SF-100 enrollment (docs/ENROLLMENT.md).

  claims.py generate N OUT_DIR
      Writes N claim keys (claim-0001.key …, 32-byte hex ed25519 seeds,
      mode 0600) with `suifleet --provision` and OUT_DIR/claims.json
      (address per key file). Put one key on each device's provisioning
      media; keep OUT_DIR offline.

  claims.py generate --roster devices.csv OUT_DIR [--template device-{n:4}]
            [--policy POLICY --manifest deployment.json]
      One claim key per CSV row (header: serial, optional alias/name and
      group). Blank aliases come from --template ({n:4}: row number
      zero-padded, {serial}). Writes OUT_DIR/enrollment/SERIAL/claim.key and,
      with --policy/--manifest, that folder's enroll.toml and deployment.json
      for `suifleet --enroll`. OUT_DIR/roster.json holds the public
      reservations (serial, alias, group, claim address): import it in the
      dApp's Enrollment page, which registers the claims and later binds each
      enrolled device's alias, serial and group. Rejects malformed and
      duplicate serials/aliases before writing anything.

  claims.py ptb OUT_DIR --package PKG --policy POLICY [--fund-mist 20000000]
      Prints `sui client ptb` commands that add the claim addresses to the
      policy and fund each with --fund-mist (the enrollment gas it will
      sponsor), at most 250 claims per transaction. Run them as the policy's
      operator.

Never transmits keys; roster.json and the PTBs contain addresses only.
"""
import argparse
import csv
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

DAEMON = Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/suifleet"
# Matches dapp/src/lib/enrollment.ts: safe in TOML, file names and FAT media.
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CHUNK = 250
MAX_ROWS = 100_000


def expand(template, index, serial):
    def token(match):
        if match.group(1) == "serial":
            return serial
        return str(index).zfill(int(match.group(2) or 0))
    return re.sub(r"\{(n(?::(\d{1,2}))?|serial)\}", token, template)


def read_roster(path, template, start):
    """Rows (serial, alias, group) and line-numbered problems."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = [h.strip().lower() for h in next(reader, [])]
        if "serial" not in header:
            return [], ["line 1: the header must name a serial column"]
        alias_at = header.index("alias") if "alias" in header else header.index("name") if "name" in header else None
        group_at = header.index("group") if "group" in header else None
        rows, problems, serials, aliases = [], [], set(), set()
        for number, cells in enumerate(reader):
            if not any(c.strip() for c in cells):
                continue
            line = number + 2
            cell = lambda at: cells[at].strip() if at is not None and at < len(cells) else ""
            serial = cell(header.index("serial"))
            alias = cell(alias_at) or (expand(template, start + len(rows) + len(problems), serial) if template else "")
            if not NAME.match(serial):
                problems.append(f"line {line}: invalid serial {serial!r}")
            elif not NAME.match(alias):
                problems.append(f"line {line}: invalid or missing alias {alias!r}")
            elif serial.lower() in serials or alias.lower() in aliases:
                problems.append(f"line {line}: duplicate serial or alias ({serial}, {alias})")
            else:
                serials.add(serial.lower())
                aliases.add(alias.lower())
                rows.append({"serial": serial, "alias": alias, **({"group": cell(group_at)} if cell(group_at) else {})})
            if len(rows) > MAX_ROWS:
                return [], [f"at most {MAX_ROWS} rows per roster"]
    return rows, problems


def enroll_toml(row, policy, keypair_path):
    return "\n".join([
        f"# SuiFleet enrollment for {row['alias']} (serial {row['serial']}).",
        "# claim.key is a single-use enrollment credential: keep this media private",
        "# and erase it after enrollment. The device generates its own identity key.",
        'manifest_path = "deployment.json"',
        f'policy_id = "{policy}"',
        'claim_key_path = "claim.key"',
        f'name = "{row["alias"]}"',
        f'keypair_path = "{keypair_path}"',
        "",
    ])


def provision(daemon, key):
    return subprocess.check_output([str(daemon), "--provision", str(key)], text=True).strip()


def generate(count, out, daemon):
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    claims = {}
    for index in range(1, count + 1):
        key = out / f"claim-{index:04d}.key"
        claims[key.name] = provision(daemon, key)
    (out / "claims.json").write_text(json.dumps(claims, indent=2) + "\n")
    print(f"{count} claim keys in {out} (addresses in claims.json)")


def generate_roster(rows, out, daemon, policy, manifest, keypair_path, fund):
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    claims, entries = {}, []
    for row in rows:
        folder = out / "enrollment" / row["serial"]
        folder.mkdir(parents=True, exist_ok=False, mode=0o700)
        key = folder / "claim.key"
        address = provision(daemon, key)
        key.chmod(0o600)
        if policy:
            (folder / "enroll.toml").write_text(enroll_toml(row, policy, keypair_path))
        if manifest:
            shutil.copyfile(manifest, folder / "deployment.json")
        claims[str(key.relative_to(out))] = address
        entries.append({**row, "claim": address})
    (out / "claims.json").write_text(json.dumps(claims, indent=2) + "\n")
    (out / "roster.json").write_text(json.dumps({"schema": 1, "claimFundMist": str(fund), "entries": entries}, indent=2) + "\n")
    print(f"{len(rows)} claim keys under {out}/enrollment; public reservations in {out}/roster.json")


def ptb(out, package, policy, fund):
    addresses = list(json.loads((out / "claims.json").read_text()).values())
    for start in range(0, len(addresses), CHUNK):
        part = addresses[start:start + CHUNK]
        arguments = ["--move-call", f"{package}::enrollment::add_claims", f"@{policy}",
                     "vector[" + ", ".join(f"@{a}" for a in part) + "]"]
        if fund:
            arguments += ["--split-coins", "gas", "[" + ", ".join([str(fund)] * len(part)) + "]", "--assign", "fees"]
            for index, address in enumerate(part):
                arguments += ["--transfer-objects", f"[fees.{index}]", f"@{address}"]
        print("sui client ptb " + " ".join(shlex.quote(a) for a in arguments) + " --gas-budget 200000000")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    g = commands.add_parser("generate")
    g.add_argument("count", type=int, nargs="?")
    g.add_argument("out", type=Path)
    g.add_argument("--daemon", type=Path, default=DAEMON)
    g.add_argument("--roster", type=Path, help="CSV with serial[,alias|name][,group] columns")
    g.add_argument("--template", default="", help="alias for rows without one, e.g. pi-{n:5} or pi-{serial}")
    g.add_argument("--start", type=int, default=1, help="first {n}")
    g.add_argument("--policy", help="write enroll.toml for this EnrollmentPolicy")
    g.add_argument("--manifest", type=Path, help="deployment manifest to copy beside each enroll.toml")
    g.add_argument("--keypair-path", default="/etc/suifleet/device.key", help="device key path in enroll.toml")
    g.add_argument("--fund-mist", type=int, default=20_000_000, help="enrollment gas recorded in roster.json")
    p = commands.add_parser("ptb")
    p.add_argument("out", type=Path)
    p.add_argument("--package", required=True)
    p.add_argument("--policy", required=True)
    p.add_argument("--fund-mist", type=int, default=20_000_000)
    args = parser.parse_args()
    if args.command == "ptb":
        ptb(args.out, args.package, args.policy, args.fund_mist)
    elif args.roster:
        if args.count is not None:
            parser.error("give either N or --roster")
        if args.policy and not re.match(r"^0x[0-9a-fA-F]{1,64}$", args.policy):
            parser.error("--policy must be an object ID")
        if not 0 < args.fund_mist <= 100_000_000:
            parser.error("--fund-mist must be 1-100000000 (0.1 SUI)")
        rows, problems = read_roster(args.roster, args.template, args.start)
        if problems:
            sys.exit("roster rejected:\n  " + "\n  ".join(problems[:50]))
        if not rows:
            sys.exit("roster has no rows")
        generate_roster(rows, args.out, args.daemon, args.policy, args.manifest, args.keypair_path.replace('"', ""), args.fund_mist)
    else:
        if args.count is None or not 1 <= args.count <= 500:
            parser.error("count must be 1-500 (or use --roster)")
        generate(args.count, args.out, args.daemon)
