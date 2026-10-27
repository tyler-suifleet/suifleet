#!/usr/bin/env python3
"""Confirm a published suifleet.io matches a reviewed build (SF-172).

    python3 site/verify_served.py site/dist https://suifleet.io

Read-only: fetches every file in the build's SHA256SUMS from the live origin
and compares digests, then checks the security response headers on the home
page. It publishes nothing. Use it after an authorized upload and record the
output in the release issue (site/README.md, "Release process").
"""
import argparse
import hashlib
from pathlib import Path
import sys
import urllib.request

HEADERS = {
    "content-security-policy": "frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "strict-transport-security": "max-age=",
}


def served_path(name):
    """The URL path a static host serves a built file under."""
    if name == "index.html":
        return "/"
    if name.endswith("/index.html"):
        return "/" + name[: -len("index.html")]
    return "/" + name


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "suifleet-site-verify"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(), {k.lower(): v for k, v in response.headers.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("build", type=Path)
    parser.add_argument("origin")
    args = parser.parse_args()
    origin = args.origin.rstrip("/")
    if not origin.startswith("https://"):
        sys.exit("origin must be https")
    problems = []
    names = []
    for line in (args.build / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        names.append(name)
        if name in ("_headers", "404.html"):
            continue  # Host configuration and the error page are not served at their own path.
        try:
            body, _ = fetch(origin + served_path(name))
        except Exception as error:  # noqa: BLE001 - report every failure, keep going
            problems.append(f"{name}: fetch failed: {error}")
            continue
        if hashlib.sha256(body).hexdigest() != digest:
            problems.append(f"{name}: served bytes differ from the reviewed build")
    _, headers = fetch(origin + "/")
    for header, expected in HEADERS.items():
        if expected not in headers.get(header, ""):
            problems.append(f"home page header {header} missing or lacks {expected!r}")
    for problem in problems:
        print("ERROR:", problem)
    if problems:
        sys.exit(f"{len(problems)} problem(s): {origin} does not match {args.build}")
    print(f"{origin} serves the reviewed build ({len(names)} files listed) with the expected headers")


if __name__ == "__main__":
    main()
