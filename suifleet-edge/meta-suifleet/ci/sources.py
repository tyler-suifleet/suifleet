#!/usr/bin/env python3
"""Verify the exact public source inputs and their relationship to Cargo.lock."""
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
from urllib.request import urlopen

LAYER = Path(__file__).resolve().parents[1]


def field(text, name, size=None):
    values = re.findall(r'^' + re.escape(name) + r' = "([^"]+)"$', text, re.M)
    if len(values) != 1 or (size and not re.fullmatch(f"[0-9a-f]{{{size}}}", values[0])):
        raise ValueError(f"Invalid or missing {name}")
    return values[0]


def download(recipe, artifact, variable, revision, checksum):
    host = r"https://raw\.githubusercontent\.com/tyler-suifleet/meta-suifleet/[0-9a-f]{40}/"
    path = re.escape(f"{artifact}-${{{variable}}}.tar.gz")
    matches = re.findall(host + path + r"(?=;)", recipe)
    if len(matches) != 1:
        raise ValueError(f"{artifact} requires an immutable public source URL")
    url = matches[0].replace("${" + variable + "}", revision)
    with urlopen(url, timeout=90) as response:
        payload = response.read(64 * 1024 * 1024 + 1)
    if len(payload) > 64 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != checksum:
        raise ValueError(f"{artifact} archive hash mismatch")
    print(f"{artifact}: {revision} {checksum}")
    return tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz")


def member(archive, path):
    entry = archive.getmember(path)
    if not entry.isfile():
        raise ValueError(f"{path} is not a regular archive file")
    return archive.extractfile(entry).read()


def check():
    edge = LAYER / "recipes-edge/suifleet"
    daemon = (edge / "suifleet_1.0.bb").read_text()
    codec = (LAYER / "recipes-edge/suifleet-walrus-codec/suifleet-walrus-codec_1.0.bb").read_text()
    revision = field(daemon, "SRCREV", 40)
    if revision != field(codec, "SUIFLEET_CODEC_SOURCE_REV", 40):
        raise ValueError("Daemon and codec source revisions differ")
    with download(daemon, "suifleet-edge", "SRCREV", revision,
                  field(daemon, "SRC_URI[edge.sha256sum]", 64)) as archive:
        lock = member(archive, "suifleet-edge/Cargo.lock")
        member(archive, "suifleet-edge/windows-runtime/Cargo.toml")
        if hashlib.sha256(lock).hexdigest() != field(daemon, "SUIFLEET_LOCK_SHA256", 64):
            raise ValueError("Cargo.lock SHA-256 differs from daemon recipe")
    expected = {}
    for package in lock.decode().split("[[package]]")[1:]:
        if re.search(r'^source = "registry\+', package, re.M):
            slug = field(package, "name") + "-" + field(package, "version")
            expected[slug] = field(package, "checksum", 64)
    include = (edge / "suifleet-crates.inc").read_text()
    hashes = re.findall(r'^SRC_URI\[([^\]]+)\.sha256sum\] = "([a-f0-9]{64})"$', include, re.M)
    sources = [f"{name}-{version}" for name, version in
               re.findall(r"crate://crates\.io/([^/\s]+)/([^\s]+)", include)]
    if not expected or len(hashes) != len(expected) or dict(hashes) != expected or sorted(sources) != sorted(expected):
        raise ValueError("Crate URI/checksum inventory differs from Cargo.lock")
    print(f"Cargo.lock and {len(expected)} crate checksums agree")
    with download(codec, "suifleet-walrus-codec", "SUIFLEET_CODEC_SOURCE_REV", revision,
                  field(codec, "SRC_URI[sha256sum]", 64)) as archive:
        pins = dict(re.findall(r'^SUIFLEET_CODEC_SHA256\[([^\]]+)\] = "([0-9a-f]{64})"$', codec, re.M))
        files = [entry.name for entry in archive if entry.isfile()]
        if sorted(pins) != sorted(files):
            raise ValueError("Codec archive inventory differs from recipe")
        for name, digest in pins.items():
            if hashlib.sha256(member(archive, name)).hexdigest() != digest:
                raise ValueError(f"Codec hash mismatch for {name}")
        if json.loads(member(archive, "provenance.json"))["source_revision"] != revision:
            raise ValueError("Codec archive provenance points to another revision")
    print(f"{len(pins)} codec file checksums agree")


if __name__ == "__main__":
    check()
