#!/usr/bin/env python3
"""Bulk claim keys for SF-021 enrollment (docs/ENROLLMENT.md).

  claims.py generate N OUT_DIR
      Writes N claim keys (claim-0001.key …, 32-byte hex ed25519 seeds,
      mode 0600) with `suifleet --provision` and OUT_DIR/claims.json
      (address per key file). Put one key on each device's provisioning
      media; keep OUT_DIR offline.

  claims.py ptb OUT_DIR --package PKG --policy POLICY [--fund-mist 20000000]
      Prints the `sui client ptb` arguments that add every claim address to
      the policy and fund each with --fund-mist (the enrollment gas it will
      sponsor) in ONE transaction. Run it as the policy's operator.

Never transmits keys; the PTB contains addresses only.
"""
import argparse
import json
import shlex
import subprocess
from pathlib import Path

DAEMON = Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/suifleet"


def generate(count, out, daemon):
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    claims = {}
    for index in range(1, count + 1):
        key = out / f"claim-{index:04d}.key"
        address = subprocess.check_output([str(daemon), "--provision", str(key)], text=True).strip()
        claims[key.name] = address
    (out / "claims.json").write_text(json.dumps(claims, indent=2) + "\n")
    print(f"{count} claim keys in {out} (addresses in claims.json)")


def ptb(out, package, policy, fund):
    addresses = list(json.loads((out / "claims.json").read_text()).values())
    arguments = ["--move-call", f"{package}::enrollment::add_claims", f"@{policy}",
                 "vector[" + ", ".join(f"@{a}" for a in addresses) + "]"]
    if fund:
        arguments += ["--split-coins", "gas", "[" + ", ".join([str(fund)] * len(addresses)) + "]", "--assign", "fees"]
        for index, address in enumerate(addresses):
            arguments += ["--transfer-objects", f"[fees.{index}]", f"@{address}"]
    print("sui client ptb " + " ".join(shlex.quote(a) for a in arguments) + " --gas-budget 100000000")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    g = commands.add_parser("generate")
    g.add_argument("count", type=int)
    g.add_argument("out", type=Path)
    g.add_argument("--daemon", type=Path, default=DAEMON)
    p = commands.add_parser("ptb")
    p.add_argument("out", type=Path)
    p.add_argument("--package", required=True)
    p.add_argument("--policy", required=True)
    p.add_argument("--fund-mist", type=int, default=20_000_000)
    args = parser.parse_args()
    if args.command == "generate":
        if not 1 <= args.count <= 500:
            parser.error("count must be 1-500 (one PTB funds at most a few hundred claims)")
        generate(args.count, args.out, args.daemon)
    else:
        ptb(args.out, args.package, args.policy, args.fund_mist)
