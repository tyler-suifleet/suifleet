#!/usr/bin/env python3
"""Report anonymous public-layer metadata and revision-bound submission evidence."""

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

EVIDENCE = Path(__file__).resolve().parents[1] / "docs/validation/yocto-layer-index.json"
RECIPES = {"suifleet", "suifleet-node", "suifleet-walrus-codec"}


def git(directory, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false")
    return subprocess.run(
        ["git", "-C", str(directory), "-c", "credential.helper=", *args],
        env=env, check=True, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=120,
    ).stdout.strip()


def metadata(directory, revision, series, maintainer):
    def read(path):
        mode = git(directory, "ls-tree", revision, "--", path).split()
        if not mode or mode[0] not in {"100644", "100755"}:
            raise ValueError(f"{path} must be a regular committed file")
        return git(directory, "show", f"{revision}:{path}")

    conf = read("conf/layer.conf")
    if 'BBFILES += "${LAYERDIR}/recipes-*/*/*.bb"' not in conf.splitlines():
        raise ValueError("recipe discovery declaration changed")
    expected = {
        "BBFILE_COLLECTIONS": {"meta-suifleet"},
        "LAYERDEPENDS_meta-suifleet": {"core", "openembedded-layer"},
        "LAYERSERIES_COMPAT_meta-suifleet": {series},
    }
    for key, required in expected.items():
        assignments = re.findall(rf'^\s*{key}\s*(?:\+=|=)\s*"([^"]*)"\s*$', conf, re.M)
        if len(assignments) != 1 or not required.issubset(assignments[0].split()):
            raise ValueError(f"{key} must declare {', '.join(sorted(required))}")
        if key == "LAYERDEPENDS_meta-suifleet" and set(assignments[0].split()) != required:
            raise ValueError("dependency declaration changed; update submission fields")

    readme = read("README.md")
    if f"Maintainer: {maintainer}" not in readme or "## Contributions" not in readme:
        raise ValueError("README maintainer or contribution instructions changed")
    if "MIT" not in readme or "Permission is hereby granted" not in read("COPYING.MIT"):
        raise ValueError("MIT metadata license is missing")
    paths = git(directory, "ls-tree", "-r", "--name-only", revision).splitlines()
    recipes = sorted(path for path in paths if path.endswith(".bb"))
    for recipe in recipes:
        read(recipe)
    if (len(recipes) != len(RECIPES)
            or {Path(path).stem.split("_", 1)[0] for path in recipes} != RECIPES
            or any(not Path(path).match("recipes-*/*/*.bb") for path in recipes)):
        raise ValueError("recipe inventory changed; update expected index records")
    return recipes


def inspect(directory, evidence):
    repository = evidence["repository"]
    git(directory, "init", "--quiet", "--template=")
    report = {}
    for branch, recorded in evidence["series"].items():
        ref = "refs/heads/" + branch
        entry = {"evidence": recorded}
        report[branch] = entry
        try:
            git(directory, "fetch", "--quiet", "--no-tags", "--depth=1",
                repository, f"{ref}:{ref}")
            revision = git(directory, "rev-parse", ref)
        except subprocess.SubprocessError as error:
            entry["fetch_error"] = getattr(error, "stderr", None) or str(error)
            continue
        entry.update(
            revision=revision,
            source=f"{repository.removesuffix('.git')}/tree/{revision}",
            revision_matches_evidence=revision == recorded["revision"],
            layer_check_revision_matches=revision == recorded["layer_check_revision"],
        )
        try:
            entry["recipes"] = metadata(directory, revision, branch, evidence["maintainer"])
        except (ValueError, subprocess.CalledProcessError) as error:
            entry["metadata_error"] = str(error)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    evidence = json.loads(EVIDENCE.read_text())
    report = {
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "repository": evidence["repository"],
        "submission": evidence["submission"],
        "source_archives": evidence["source_archives"],
        "pending_final_inputs": evidence["pending_final_inputs"],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-layer-index-") as directory:
            report["series"] = inspect(directory, evidence)
    except (OSError, subprocess.SubprocessError) as error:
        report["fetch_error"] = getattr(error, "stderr", None) or str(error)
    print(json.dumps(report, indent=2))
    return int("fetch_error" in report or any(
        branch["evidence"]["candidate"] and (
            "metadata_error" in branch or not branch.get("revision_matches_evidence")
        ) for branch in report.get("series", {}).values()
    ))


if __name__ == "__main__":
    raise SystemExit(main())
