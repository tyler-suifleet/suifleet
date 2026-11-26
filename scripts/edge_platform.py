#!/usr/bin/env python3
"""Inspect edge compatibility profiles or require a named, scoped check."""

import argparse
import json
from pathlib import Path

MATRIX = Path(__file__).resolve().parents[1] / "kas/edge-platforms.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", nargs="?", help="omit to list all profiles")
    parser.add_argument("--require-check", help="require a registered automated check for the selected profile")
    args = parser.parse_args()
    profiles = json.loads(MATRIX.read_text())["profiles"]
    if args.profile is None:
        if args.require_check is not None:
            parser.error("--require-check needs a profile")
        for name, profile in profiles.items():
            print(f"{name}: {profile['status']}")
        return

    profile = profiles.get(args.profile)
    if profile is None:
        parser.error(f"unknown edge profile {args.profile!r}; available profiles: {', '.join(profiles)}")
    if args.require_check is not None and args.require_check not in profile["checks"]:
        reasons = profile.get("blockers", ["no such automated check is registered"])
        parser.error(
            f"unsupported check {args.require_check!r} for {args.profile} "
            f"({profile['status']}): {'; '.join(reasons)}"
        )
    print(json.dumps(profile, indent=2))


if __name__ == "__main__":
    main()
