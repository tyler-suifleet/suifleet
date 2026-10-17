#!/usr/bin/env python3
"""Disk-pressure audit and bounded cleanup of regenerable outputs (SF-080).

  audit  per-category usage as JSON: filesystems against budgets, ticket
         worktrees by project and coordinator status (with their regenerable
         outputs), configured Yocto trees, Docker (logical, overlapping) and
         extra report-only paths. Read-only.
  plan   dry run: which allowlisted outputs are eligible now and why every
         other one is not. Read-only.
  apply  removes only the plan's candidates that still pass every check,
         re-evaluated immediately before each removal, and records the bytes
         actually reclaimed.

Eligible worktree outputs: an allowlisted relative path in the worktree of a
ticket the coordinator reports `done`, inactive for `retain_days`, ignored and
untracked by Git, with no symlink on its path, no protected file inside, no
resource lock naming the ticket, no process using it and no running container
mounting it. Yocto temporary work is eligible only where a tree is configured
explicitly, with the same idle, lock, process and container checks and no
`bitbake.lock`. Docker storage, volumes, downloads and anything not allowlisted
are never removed. Any check that cannot be completed fails closed.
"""
import argparse
import fnmatch
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

SCHEMA = 1
DEFAULT_CONFIG = Path.home() / ".config/suifleet/disk-cleanup.json"
DEFAULT_OUTPUTS = ["suifleet-edge/target", "suifleet-indexer/target", "dapp/node_modules", "dapp/.next",
                   "contracts/build", "target", "node_modules"]
# Files that must never be inside something we delete: keys, wallets, journals,
# enrollment/device configuration and deployment state.
PROTECTED = ["*.key", "*.pem", "*.keystore", "sui.keystore", "client.yaml", ".env", ".env.*", "*journal*",
             "config.toml", "enroll*.toml", "signer.toml", "deployment.json", "github-state.json", "*.sqlite3",
             "*.mnemonic", "*recovery*"]
YOCTO_NEVER = {"deploy", "conf", "downloads", "sstate-cache", "cache", "log", "buildhistory"}


class Unverifiable(RuntimeError):
    """A safety check could not be completed: fail closed."""


# --- Environment: everything outside the filesystem walk, injectable in tests ----

class System:
    def __init__(self, config):
        self.config = config

    def ticket(self, project, *args):
        result = subprocess.run(["ticket", "--project", project["id"], *args], cwd=project["root"],
                                text=True, capture_output=True, timeout=120)
        if result.returncode:
            raise Unverifiable(f"ticket {' '.join(args)} for {project['id']}: {result.stderr.strip()[-200:]}")
        return json.loads(result.stdout)

    def claims(self, project):
        return self.ticket(project, "list")

    def locks(self, project):
        return self.ticket(project, "locks")

    def processes(self):
        """(paths in use by readable processes, count of unreadable processes)."""
        paths, unreadable = [], 0
        for pid in filter(str.isdigit, os.listdir("/proc")):
            if int(pid) == os.getpid():
                continue
            base = f"/proc/{pid}"
            try:
                found = [os.readlink(f"{base}/cwd"), os.readlink(f"{base}/root")]
                for fd in os.listdir(f"{base}/fd"):
                    try:
                        found.append(os.readlink(f"{base}/fd/{fd}"))
                    except OSError:
                        pass
                with open(f"{base}/maps") as maps:
                    found += [line.split(None, 5)[5].strip() for line in maps if len(line.split(None, 5)) == 6]
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                unreadable += 1
                continue
            paths += [p.removesuffix(" (deleted)") for p in found if p.startswith("/")]
        return paths, unreadable

    def container_mounts(self):
        ids = subprocess.run(["docker", "ps", "-q"], text=True, capture_output=True, timeout=60)
        if ids.returncode:
            raise Unverifiable(f"docker ps: {ids.stderr.strip()[-200:]}")
        if not ids.stdout.split():
            return []
        inspect = subprocess.run(["docker", "inspect", *ids.stdout.split()], text=True, capture_output=True, timeout=60)
        if inspect.returncode:
            raise Unverifiable(f"docker inspect: {inspect.stderr.strip()[-200:]}")
        return [m["Source"] for c in json.loads(inspect.stdout) for m in c.get("Mounts", []) if m.get("Source")]

    def docker_usage(self):
        result = subprocess.run(["docker", "system", "df", "--format", "{{json .}}"], text=True, capture_output=True, timeout=120)
        if result.returncode:
            return {"available": False, "error": result.stderr.strip()[-200:]}
        root = subprocess.run(["docker", "info", "--format", "{{.DockerRootDir}}"], text=True, capture_output=True, timeout=60).stdout.strip()
        return {"available": True, "root": root, "logical": [json.loads(line) for line in result.stdout.splitlines() if line.strip()]}

    def now(self):
        return time.time()


# --- Filesystem helpers --------------------------------------------------------------

def measure(path, protected=()):
    """Physical bytes (allocated blocks, hard links once), file count, protected
    files found, unreadable entries and newest mtime; never follows symlinks."""
    seen, total, files, found, unreadable, newest = set(), 0, 0, [], 0, 0.0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            unreadable += 1
            continue
        with entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    unreadable += 1
                    continue
                newest = max(newest, info.st_mtime)
                if (info.st_dev, info.st_ino) not in seen:
                    seen.add((info.st_dev, info.st_ino))
                    total += info.st_blocks * 512
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                else:
                    files += 1
                    if protected and any(fnmatch.fnmatch(entry.name, p) for p in protected) and len(found) < 20:
                        found.append(entry.path)
    return {"bytes": total, "files": files, "protected": found, "unreadable": unreadable, "newest_mtime": newest}


def inside(path, parent):
    path, parent = os.path.normpath(path), os.path.normpath(parent)
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def plain_relative(relative):
    return bool(relative) and not os.path.isabs(relative) and all(part not in ("", ".", "..") for part in relative.split("/"))


def safe_child(base, relative):
    """The child path if `relative` is a plain relative path whose every
    component exists and is not a symlink; otherwise the refusal reason."""
    if not plain_relative(relative):
        return None, "allowlist entry must be a plain relative path"
    current = Path(base)
    if current.is_symlink() or not current.is_dir():
        return None, "root is a symlink or missing"
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            return None, f"symlink on path: {current}"
        if not current.exists():
            return None, "absent"
    if not current.is_dir():
        return None, "not a directory"
    if os.path.realpath(current) != os.path.normpath(current) or not inside(os.path.realpath(current), os.path.realpath(base)):
        return None, "path escapes its root"
    return current, None


def git_state(worktree, relative):
    """Refusal reason unless the path is ignored and contains no tracked files."""
    tracked = subprocess.run(["git", "-C", str(worktree), "ls-files", "-z", "--", relative], capture_output=True, timeout=120)
    if tracked.returncode:
        raise Unverifiable(f"git ls-files in {worktree}: {tracked.stderr.decode()[-200:]}")
    if tracked.stdout:
        return "contains files tracked by Git"
    ignored = subprocess.run(["git", "-C", str(worktree), "check-ignore", "-q", "--", relative], capture_output=True, timeout=60)
    if ignored.returncode == 1:
        return "not ignored by Git"
    if ignored.returncode:
        raise Unverifiable(f"git check-ignore in {worktree}: {ignored.stderr.decode()[-200:]}")
    return None


def git_activity(worktree):
    """Newest mtime of the worktree's Git metadata (index, HEAD, reflog)."""
    marker = Path(worktree) / ".git"
    gitdir = marker
    if marker.is_file():
        text = marker.read_text().strip()
        if text.startswith("gitdir:"):
            gitdir = (Path(worktree) / text.split(":", 1)[1].strip()).resolve()
    times = [p.stat().st_mtime for p in (gitdir / "index", gitdir / "HEAD", gitdir / "logs/HEAD") if p.exists()]
    return max(times, default=0.0)


def filesystems(paths, budget):
    seen, out = set(), []
    for path in paths:
        try:
            info = os.statvfs(path)
            device = os.stat(path).st_dev
        except OSError:
            continue
        if device in seen:
            continue
        seen.add(device)
        size, free = info.f_blocks * info.f_frsize, info.f_bavail * info.f_frsize
        used = (size - free) / size if size else 0
        level = "ok"
        if free < budget.get("critical_free_bytes", 0) or used >= budget.get("critical_used_fraction", 0.97):
            level = "critical"
        elif free < budget.get("warn_free_bytes", 0) or used >= budget.get("warn_used_fraction", 0.9):
            level = "warn"
        out.append({"path": path, "size_bytes": size, "free_bytes": free, "used_fraction": round(used, 4), "pressure": level})
    return out


# --- Policy ------------------------------------------------------------------------

def load_config(path):
    config = json.loads(Path(path).read_text())
    if config.get("schema_version") != SCHEMA:
        raise SystemExit(f"{path}: schema_version must be {SCHEMA}")
    for entry in config.get("worktree_outputs", DEFAULT_OUTPUTS):
        if not plain_relative(entry):
            raise SystemExit(f"{path}: worktree_outputs entry {entry!r} is not a plain relative path")
    for tree in config.get("yocto", []):
        for entry in tree.get("retire", []):
            parts = entry.split("/")
            if len(parts) != 2 or not parts[0].startswith("tmp") or parts[1] not in ("work", "work-shared"):
                raise SystemExit(f"{path}: yocto retire entry {entry!r} must be tmp*/work or tmp*/work-shared")
    return config


class Context:
    """One snapshot of claims, locks, processes and mounts."""

    def __init__(self, config, system):
        self.config, self.system = config, system
        self.claims, self.locks, self.errors = {}, [], []
        for project in config.get("projects", []):
            try:
                self.claims[project["id"]] = system.claims(project)
            except Unverifiable as error:
                self.errors.append(str(error))
            try:
                self.locks += system.locks(project)
            except Unverifiable as error:
                self.errors.append(str(error))
        self.process_paths, self.unreadable_processes = system.processes()
        try:
            self.mounts = system.container_mounts()
        except Unverifiable as error:
            self.mounts, self.errors = None, self.errors + [str(error)]
        self.locks = [l for i, l in enumerate(self.locks) if l not in self.locks[:i]]

    def worktrees(self):
        for project in self.config.get("projects", []):
            for claim in self.claims.get(project["id"], []):
                if claim.get("worktree"):
                    yield project, claim

    def lock_naming(self, *needles):
        """Locks whose resource, ticket, purpose or session mention a needle.
        Expired locks still count: they stay reserved until an operator
        breaks them after checking the holder has stopped."""
        needles = [n.lower() for n in needles if n]
        now = self.system.now()
        return [l["resource"] + (" (expired; break-lock after verifying)" if l.get("expires", now + 1) < now else "") for l in self.locks
                if any(n in json.dumps({k: l.get(k) for k in ("resource", "ticket", "purpose", "session")}).lower() for n in needles)]

    def in_use(self, path, also_cwd_under=None):
        """Why the path is busy, or None."""
        for used in self.process_paths:
            if inside(used, path) or (also_cwd_under and inside(used, also_cwd_under)):
                return f"in use by a process ({used})"
        if self.mounts is None:
            return "container mounts unverifiable"
        for source in self.mounts:
            if inside(source, path) or inside(path, source):
                return f"mounted by a running container ({source})"
        return None


def evaluate_output(ctx, project, claim, relative, now, scan=True):
    """(candidate dict, None) when eligible, else (record, reason)."""
    worktree = claim["worktree"]
    record = {"kind": "worktree-output", "project": project["id"], "ticket": claim["key"], "status": claim["status"],
              "path": os.path.join(worktree, relative), "worktree": worktree, "relative": relative}
    path, reason = safe_child(worktree, relative)
    if reason:
        return record, reason
    if claim["status"] != "done":
        return record, f"ticket status {claim['status']}"
    if claim.get("publish_pending"):
        return record, "coordinator publication pending"
    if ctx.errors:
        return record, "unverifiable: " + "; ".join(ctx.errors)
    retain = ctx.config.get("retain_days", 3) * 86400
    idle = now - git_activity(worktree)
    if idle < retain:
        return record, f"worktree active {idle / 86400:.1f} days ago (retain {retain / 86400:g})"
    locks = ctx.lock_naming(claim["key"], worktree, os.path.basename(worktree))
    if locks:
        return record, f"resource locks name it: {', '.join(locks)}"
    busy = ctx.in_use(str(path), also_cwd_under=worktree)
    if busy:
        return record, busy
    reason = git_state(worktree, relative)
    if reason:
        return record, reason
    if scan:
        measured = measure(path, ctx.config.get("protected", PROTECTED))
        record.update(bytes=measured["bytes"], files=measured["files"])
        if measured["protected"]:
            return record, f"protected files inside: {measured['protected'][:3]}"
        if measured["unreadable"]:
            return record, f"{measured['unreadable']} unreadable entries"
        if now - measured["newest_mtime"] < retain:
            return record, "recently modified inside"
    return record, None


def yocto_activity(build):
    times = []
    for name in ("bitbake-cookerdaemon.log", "bitbake.lock"):
        if (build / name).exists():
            times.append((build / name).stat().st_mtime)
    for tmp in build.glob("tmp*"):
        for sub in ("log", "stamps", "deploy"):
            if (tmp / sub).exists():
                times.append((tmp / sub).stat().st_mtime)
    return max(times, default=0.0)


def evaluate_yocto(ctx, tree, entry, now, scan=True):
    build = Path(tree["path"])
    record = {"kind": "yocto-work", "tree": str(build), "path": str(build / entry), "relative": entry}
    path, reason = safe_child(build, entry)
    if reason:
        return record, reason
    if ctx.errors:
        return record, "unverifiable: " + "; ".join(ctx.errors)
    if (build / "bitbake.lock").exists():
        return record, "bitbake.lock present (a build may be running)"
    retain = tree.get("retain_days", ctx.config.get("retain_days", 3)) * 86400
    idle = now - yocto_activity(build)
    if idle < retain:
        return record, f"tree active {idle / 86400:.1f} days ago (retain {retain / 86400:g})"
    for keep in tree.get("preserve", []):
        if inside(os.path.join(build, keep), str(path)):
            return record, f"contains preserved path {keep}"
    locks = ctx.lock_naming(str(build), build.parent.name, "yocto", "bitbake")
    if locks:
        return record, f"resource locks may use it: {', '.join(sorted(set(locks)))}"
    busy = ctx.in_use(str(path), also_cwd_under=str(build))
    if busy:
        return record, busy
    if scan:
        measured = measure(path, ctx.config.get("protected", PROTECTED))
        record.update(bytes=measured["bytes"], files=measured["files"])
        if measured["protected"] and not tree.get("allow_protected_names"):
            return record, f"protected files inside: {measured['protected'][:3]}"
    return record, None


def plan(config, system):
    ctx, now = Context(config, system), system.now()
    candidates, excluded = [], []
    for project, claim in ctx.worktrees():
        for relative in config.get("worktree_outputs", DEFAULT_OUTPUTS):
            if not os.path.lexists(os.path.join(claim["worktree"], relative)):
                continue
            record, reason = evaluate_output(ctx, project, claim, relative, now)
            (excluded.append({**record, "reason": reason}) if reason else candidates.append(record))
    for tree in config.get("yocto", []):
        for entry in tree.get("retire", []):
            if not os.path.lexists(os.path.join(tree["path"], entry)):
                continue
            record, reason = evaluate_yocto(ctx, tree, entry, now)
            (excluded.append({**record, "reason": reason}) if reason else candidates.append(record))
    budget = config.get("max_bytes_per_run")
    if budget:
        kept, total = [], 0
        for c in sorted(candidates, key=lambda c: -c.get("bytes", 0)):
            if total + c.get("bytes", 0) > budget:
                excluded.append({**c, "reason": "over this run's max_bytes_per_run"})
            else:
                kept.append(c)
                total += c.get("bytes", 0)
        candidates = kept
    return {"schema_version": SCHEMA, "kind": "disk-cleanup-plan", "created": now, "dry_run": True,
            "candidates": candidates, "candidate_bytes": sum(c.get("bytes", 0) for c in candidates),
            "excluded": excluded, "unverifiable": ctx.errors, "unreadable_processes": ctx.unreadable_processes}


def remove(path, record):
    """Rename aside atomically (writers then recreate a fresh output) and
    delete without following symlinks. Returns the errors."""
    if not shutil.rmtree.avoids_symlink_attacks:
        return ["this platform's rmtree is not symlink-attack safe"]
    aside = path.with_name(f".{path.name}.retiring-{os.getpid()}")
    os.rename(path, aside)
    errors = []
    shutil.rmtree(aside, onexc=lambda fn, p, exc: errors.append(f"{p}: {exc}")) if sys.version_info >= (3, 12) else \
        shutil.rmtree(aside, onerror=lambda fn, p, exc: errors.append(f"{p}: {exc[1]}"))
    record["left_aside"] = str(aside) if aside.exists() else None
    return errors


def apply(config, system, plan_data):
    if plan_data.get("kind") != "disk-cleanup-plan" or plan_data.get("schema_version") != SCHEMA:
        raise SystemExit("not a disk-cleanup plan")
    roots = sorted({c["path"] for c in plan_data["candidates"]})
    before = filesystems([os.path.dirname(r) for r in roots] or ["/"], {})
    results = []
    for planned in plan_data["candidates"]:
        # Re-evaluate everything right before this removal.
        ctx, now = Context(config, system), system.now()
        if planned["kind"] == "worktree-output":
            project = next((p for p in config.get("projects", []) if p["id"] == planned["project"]), None)
            claim = next((c for c in ctx.claims.get(planned["project"], []) if c["key"] == planned["ticket"]), None)
            if not project or not claim or claim.get("worktree") != planned["worktree"]:
                results.append({**planned, "result": "skipped", "reason": "ticket or worktree changed"})
                continue
            if planned["relative"] not in config.get("worktree_outputs", DEFAULT_OUTPUTS):
                results.append({**planned, "result": "skipped", "reason": "no longer allowlisted"})
                continue
            record, reason = evaluate_output(ctx, project, claim, planned["relative"], now)
        else:
            tree = next((t for t in config.get("yocto", []) if t["path"] == planned["tree"] and planned["relative"] in t.get("retire", [])), None)
            if not tree:
                results.append({**planned, "result": "skipped", "reason": "no longer configured"})
                continue
            record, reason = evaluate_yocto(ctx, tree, planned["relative"], now)
        if reason:
            results.append({**record, "result": "skipped", "reason": reason})
            continue
        path = Path(record["path"])
        try:
            errors = remove(path, record)
        except OSError as error:
            results.append({**record, "result": "failed", "reason": str(error)})
            continue
        remaining = measure(Path(record["left_aside"]))["bytes"] if record.get("left_aside") else 0
        results.append({**record, "result": "partial" if errors else "removed", "freed_bytes": record.get("bytes", 0) - remaining,
                        "errors": errors[:10], "error_count": len(errors)})
    after = filesystems([b["path"] for b in before], {})
    return {"schema_version": SCHEMA, "kind": "disk-cleanup-result", "finished": system.now(), "results": results,
            "measured_freed_bytes": sum(r.get("freed_bytes", 0) for r in results),
            "filesystem_before": before, "filesystem_after": after,
            "filesystem_free_delta_bytes": sum(a["free_bytes"] for a in after) - sum(b["free_bytes"] for b in before),
            "note": "Free-space deltas include unrelated concurrent writes. Cleanup establishes no build or test result."}


def audit(config, system):
    ctx = Context(config, system)
    outputs = config.get("worktree_outputs", DEFAULT_OUTPUTS)
    projects = {}
    for project, claim in ctx.worktrees():
        path = Path(claim["worktree"])
        if not path.is_dir():
            continue
        entry = projects.setdefault(project["id"], {})
        status = entry.setdefault(claim["status"], {"worktrees": 0, "bytes": 0, "regenerable_bytes": 0, "unreadable": 0})
        whole = measure(path)
        regenerable = sum(measure(p)["bytes"] for p in (path / o for o in outputs) if p.is_dir() and not p.is_symlink())
        status["worktrees"] += 1
        status["bytes"] += whole["bytes"]
        status["regenerable_bytes"] += regenerable
        status["unreadable"] += whole["unreadable"]
    yocto = []
    for tree in config.get("yocto", []) + [{"path": p, "report_only": True} for p in config.get("yocto_report_only", [])]:
        build = Path(tree["path"])
        if not build.is_dir():
            continue
        parts = {}
        for child in sorted(build.iterdir()):
            if child.is_dir() and not child.is_symlink():
                if child.name.startswith("tmp"):
                    for sub in sorted(child.iterdir()):
                        if sub.is_dir() and not sub.is_symlink():
                            parts[f"{child.name}/{sub.name}"] = measure(sub)["bytes"]
                else:
                    parts[child.name] = measure(child)["bytes"]
        yocto.append({"path": str(build), "configured_retire": tree.get("retire", []), "bytes": sum(parts.values()), "parts": parts,
                      "idle_days": round((system.now() - yocto_activity(build)) / 86400, 1)})
    extra = [{"path": p, **{k: v for k, v in measure(Path(p)).items() if k != "protected"}} for p in config.get("report_paths", []) if Path(p).exists()]
    roots = [p["root"] for p in config.get("projects", [])] + [t["path"] for t in config.get("yocto", [])] + config.get("report_paths", [])
    return {"schema_version": SCHEMA, "kind": "disk-audit", "created": system.now(),
            "filesystems": filesystems(roots or ["/"], config.get("budget", {})),
            "worktrees": projects, "yocto": yocto, "report_paths": extra,
            "docker": {**system.docker_usage(), "note": "Docker's logical sizes overlap (shared layers, build cache) and are not physical "
                       "usage; its root directory is not scanned (permission-limited). Docker storage is never removed by this tool."},
            "unverifiable": ctx.errors, "unreadable_processes": ctx.unreadable_processes,
            "note": "Bytes are allocated blocks with hard links counted once; entries that could not be read are counted, not sized."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("audit")
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--out", help="write the plan here (refuses to overwrite)")
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("plan")
    apply_parser.add_argument("--yes", action="store_true", help="required: actually delete")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    system = System(config)
    if args.command == "audit":
        result = audit(config, system)
    elif args.command == "plan":
        result = plan(config, system)
        if args.out:
            with open(args.out, "x") as out:
                json.dump(result, out, indent=2)
    else:
        if not args.yes:
            raise SystemExit("apply deletes files; pass --yes after reviewing the plan")
        result = apply(config, system, json.loads(Path(args.plan).read_text()))
    print(json.dumps(result, indent=2))
    if args.command == "apply" and any(r["result"] in ("failed", "partial") for r in result["results"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
