#!/usr/bin/env python3
"""Disk-pressure audit and bounded cleanup of regenerable outputs (SF-080).

  audit  per-category usage as JSON: filesystems against budgets, ticket
         worktrees by project and coordinator status (with their regenerable
         outputs), configured Yocto trees, Docker (logical, overlapping) and
         extra report-only paths. Read-only.
  plan   dry run: which allowlisted outputs are eligible now and why every
         other one is not. Read-only.
  apply  removes only the plan's candidates that still pass every check,
         re-evaluated and rescanned under the coordinator lock build:TICKET
         immediately before each removal, and records the bytes reclaimed.

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
import stat
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
        self.session = f"disk-cleanup-{os.getpid()}-{int(time.time())}"

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

    def lock(self, project, resource, ticket, purpose):
        """Take the coordinator lease writers honor; returns its token. A live
        lock held by any other session is refused (Unverifiable)."""
        args = ["lock", resource, "--agent", "Other", "--model", "disk_cleanup.py", "--session", self.session,
                "--purpose", purpose, "--ttl", "900"] + (["--ticket", ticket] if ticket else [])
        return self.ticket(project, *args)["token"]

    def renew(self, project, resource, token):
        """Extend the lease; refused (Unverifiable) once it is no longer ours."""
        self.ticket(project, "lock-renew", resource, "--lock-token", token, "--ttl", "900")

    def unlock(self, project, resource, token):
        self.ticket(project, "unlock", resource, "--lock-token", token)

    def processes(self):
        """(paths in use by inspected processes, uids of processes that could
        not be fully inspected). A process that exits mid-scan is skipped; a
        descriptor closed mid-scan is transient; any other failure, including
        a permission error on a single descriptor, makes the process
        uninspectable."""
        paths, uninspectable = [], []
        for pid in filter(str.isdigit, os.listdir("/proc")):
            if int(pid) == os.getpid():
                continue
            base = f"/proc/{pid}"
            try:
                uid = os.stat(base).st_uid
                found = [os.readlink(f"{base}/cwd"), os.readlink(f"{base}/root")]
                for fd in os.listdir(f"{base}/fd"):
                    try:
                        found.append(os.readlink(f"{base}/fd/{fd}"))
                    except FileNotFoundError:
                        pass
                with open(f"{base}/maps") as maps:
                    found += [line.split(None, 5)[5].strip() for line in maps if len(line.split(None, 5)) == 6]
            except (FileNotFoundError, ProcessLookupError):
                if os.path.exists(base):
                    uninspectable.append(uid)
                continue
            except OSError:
                uninspectable.append(uid)
                continue
            paths += [p.removesuffix(" (deleted)") for p in found if p.startswith("/")]
        return paths, uninspectable

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
    """Physical bytes (allocated blocks, hard links once), the part of them
    deleting the tree would free (`exclusive_bytes`: excludes files that keep a
    hard link outside it), file count, protected files found, unreadable
    entries and newest mtime; never follows symlinks."""
    seen, total, files, found, unreadable, newest = set(), 0, 0, [], 0, 0.0
    shared = {}  # (dev, ino) of multiply linked files -> [blocks, nlink, links seen]
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
                key, is_dir = (info.st_dev, info.st_ino), entry.is_dir(follow_symlinks=False)
                if key not in seen:
                    seen.add(key)
                    total += info.st_blocks * 512
                    if not is_dir and info.st_nlink > 1:
                        shared[key] = [info.st_blocks * 512, info.st_nlink, 0]
                if key in shared:
                    shared[key][2] += 1
                if is_dir:
                    stack.append(entry.path)
                else:
                    files += 1
                    if protected and any(fnmatch.fnmatch(entry.name, p) for p in protected) and len(found) < 20:
                        found.append(entry.path)
    outside = sum(blocks for blocks, nlink, links in shared.values() if links < nlink)
    return {"bytes": total, "exclusive_bytes": total - outside, "files": files, "protected": found,
            "unreadable": unreadable, "newest_mtime": newest}


def lstat(path):
    """The path's lstat, or None when it does not exist; other errors (such as
    permission denied) raise, so callers cannot mistake them for absence."""
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def is_plain_dir(path):
    info = lstat(path)
    return info is not None and stat.S_ISDIR(info.st_mode)


def subdirs(path, unscannable):
    """Sorted non-symlink child directories. A read error on the path or an
    entry is recorded in `unscannable` (path -> error) rather than raised."""
    found = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        found.append(Path(entry.path))
                except OSError as error:
                    unscannable[entry.path] = str(error)
    except OSError as error:
        unscannable[str(path)] = str(error)
    return sorted(found)


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
    try:
        if not is_plain_dir(current):
            return None, "root is a symlink or missing"
        for part in relative.split("/"):
            current = current / part
            info = lstat(current)
            if info is None:
                return None, "absent"
            if stat.S_ISLNK(info.st_mode):
                return None, f"symlink on path: {current}"
        if not stat.S_ISDIR(info.st_mode):
            return None, "not a directory"
    except OSError as error:
        return None, f"cannot inspect path: {error}"
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
        # The cleanup's own exclusion lock never blocks it.
        own = getattr(system, "session", None)
        self.locks = [l for l in self.locks if not own or l.get("session") != own]
        self.process_paths, self.uninspectable_uids = system.processes()
        self.unreadable_processes = len(self.uninspectable_uids)
        # Uninspectable processes may be using any candidate. Fail closed unless
        # the operator explicitly accepted that uid (never one owning a candidate).
        accepted = set(config.get("uninspectable_uids_accepted", []))
        refused = sorted(set(self.uninspectable_uids) - accepted)
        if refused:
            self.errors.append(f"{sum(u in refused for u in self.uninspectable_uids)} processes could not be inspected "
                               f"(uids {refused}; run as their owner or list a uid in uninspectable_uids_accepted)")
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
        try:
            owner = os.lstat(path).st_uid
        except OSError as error:
            return f"cannot stat: {error}"
        if owner in self.uninspectable_uids:
            return f"a process of its owner uid {owner} could not be inspected"
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
        record.update(bytes=measured["bytes"], exclusive_bytes=measured["exclusive_bytes"], files=measured["files"])
        reason = scan_refusal(measured, now, retain)
        if reason:
            return record, reason
    return record, None


def scan_refusal(measured, now, retain, allow_protected=False):
    """Why a measured tree must be kept, or None."""
    if measured["protected"] and not allow_protected:
        return f"protected files inside: {measured['protected'][:3]}"
    if measured["unreadable"]:
        return f"{measured['unreadable']} unreadable entries (contents unverifiable)"
    if now - measured["newest_mtime"] < retain:
        return "recently modified inside"
    return None


def yocto_activity(build):
    """Newest mtime of the tree's BitBake markers. Raises OSError when the tree
    or a marker cannot be read, so activity is never silently assumed absent."""
    times = []
    for name in ("bitbake-cookerdaemon.log", "bitbake.lock"):
        info = lstat(build / name)
        if info:
            times.append(info.st_mtime)
    with os.scandir(build) as entries:
        tmps = [Path(e.path) for e in entries if e.name.startswith("tmp")]
    for tmp in tmps:
        for sub in ("log", "stamps", "deploy"):
            info = lstat(tmp / sub)
            if info:
                times.append(info.st_mtime)
    return max(times, default=0.0)


def yocto_busy(build, now, retain):
    """Why the tree may be in use (lock present, recent activity, unreadable), or None."""
    try:
        if lstat(build / "bitbake.lock"):
            return "bitbake.lock present (a build may be running)"
        idle = now - yocto_activity(build)
    except OSError as error:
        return f"tree activity unverifiable: {error}"
    if idle < retain:
        return f"tree active {idle / 86400:.1f} days ago (retain {retain / 86400:g})"
    return None


def evaluate_yocto(ctx, tree, entry, now, scan=True):
    build = Path(tree["path"])
    record = {"kind": "yocto-work", "tree": str(build), "path": str(build / entry), "relative": entry}
    path, reason = safe_child(build, entry)
    if reason:
        return record, reason
    if ctx.errors:
        return record, "unverifiable: " + "; ".join(ctx.errors)
    retain = tree.get("retain_days", ctx.config.get("retain_days", 3)) * 86400
    busy = yocto_busy(build, now, retain)
    if busy:
        return record, busy
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
        record.update(bytes=measured["bytes"], exclusive_bytes=measured["exclusive_bytes"], files=measured["files"])
        reason = scan_refusal(measured, now, retain, allow_protected=tree.get("allow_protected_names"))
        if reason:
            return record, reason
    return record, None


def present(path, excluded, record):
    """Whether the path exists; one that cannot be checked is excluded, not skipped."""
    try:
        return lstat(path) is not None
    except OSError as error:
        excluded.append({**record, "path": path, "reason": f"cannot inspect path: {error}"})
        return False


def plan(config, system):
    ctx, now = Context(config, system), system.now()
    candidates, excluded = [], []
    for project, claim in ctx.worktrees():
        for relative in config.get("worktree_outputs", DEFAULT_OUTPUTS):
            if not present(os.path.join(claim["worktree"], relative), excluded, {
                    "kind": "worktree-output", "project": project["id"], "ticket": claim["key"], "relative": relative}):
                continue
            record, reason = evaluate_output(ctx, project, claim, relative, now)
            (excluded.append({**record, "reason": reason}) if reason else candidates.append(record))
    for tree in config.get("yocto", []):
        for entry in tree.get("retire", []):
            if not present(os.path.join(tree["path"], entry), excluded, {"kind": "yocto-work", "tree": tree["path"], "relative": entry}):
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
            "candidate_exclusive_bytes": sum(c.get("exclusive_bytes", 0) for c in candidates),
            "excluded": excluded, "unverifiable": ctx.errors, "unreadable_processes": ctx.unreadable_processes,
            "uninspectable_process_uids": sorted(set(ctx.uninspectable_uids))}


def delete(aside):
    """Delete a renamed-aside tree without following symlinks. Returns the errors."""
    errors = []
    shutil.rmtree(aside, onexc=lambda fn, p, exc: errors.append(f"{p}: {exc}")) if sys.version_info >= (3, 12) else \
        shutil.rmtree(aside, onerror=lambda fn, p, exc: errors.append(f"{p}: {exc[1]}"))
    return errors


def retain_seconds(config, planned):
    tree = next((t for t in config.get("yocto", []) if t["path"] == planned.get("tree")), {})
    return tree.get("retain_days", config.get("retain_days", 3)) * 86400


def exclusion_lock(config, planned):
    """(project, resource, ticket) of the coordinator lock that excludes
    cooperative writers: build:TICKET for worktree outputs, the tree's
    configured `lock` (default build:<tree parent name>) for Yocto work."""
    if planned["kind"] == "worktree-output":
        project = next((p for p in config.get("projects", []) if p["id"] == planned["project"]), None)
        return project, f"build:{planned['ticket']}", planned["ticket"]
    tree = next((t for t in config.get("yocto", []) if t["path"] == planned.get("tree")), {})
    projects = config.get("projects", [])
    return (projects[0] if projects else None), tree.get("lock", f"build:{Path(planned['tree']).parent.name}"), None


def recheck(ctx, planned, target, now, retain):
    """On a fresh snapshot, confirm nothing claimed, locked, entered or
    reactivated the candidate (now at `target`) since it was evaluated."""
    if ctx.errors:
        return "unverifiable: " + "; ".join(ctx.errors)
    if planned["kind"] == "worktree-output":
        worktree = planned["worktree"]
        claim = next((c for c in ctx.claims.get(planned["project"], []) if c["key"] == planned["ticket"]), None)
        if not claim or claim.get("worktree") != worktree:
            return "ticket or worktree changed"
        if claim["status"] != "done" or claim.get("publish_pending"):
            return f"ticket status {claim['status']}" + (" (publication pending)" if claim.get("publish_pending") else "")
        if now - git_activity(worktree) < retain:
            return "worktree became active"
        locks = ctx.lock_naming(claim["key"], worktree, os.path.basename(worktree))
        scope = worktree
    else:
        build = Path(planned["tree"])
        busy = yocto_busy(build, now, retain)
        if busy:
            return busy
        locks = ctx.lock_naming(str(build), build.parent.name, "yocto", "bitbake")
        scope = str(build)
    if locks:
        return f"resource locks name it: {', '.join(sorted(set(locks)))}"
    return ctx.in_use(str(target), also_cwd_under=scope)


def retire(config, system, planned, evaluate, budget_left):
    """Under the exclusion lock: evaluate and scan the output in place (it
    stays usable), check the run's remaining budget, confirm the lease is still
    held, recheck, rename it aside, check again, release the lock. Holding the
    lock before the scan means a cooperative writer that finished before it was
    taken left its changes for this scan to see. Returns (record, aside path to
    delete, None) or (record, None, refusal reason)."""
    project, resource, ticket = exclusion_lock(config, planned)
    if not project:
        return planned, None, "no coordinator project to take the exclusion lock in"
    try:
        token = system.lock(project, resource, ticket, f"disk cleanup retiring {planned['path']}")
    except Unverifiable as error:
        return planned, None, f"exclusion lock {resource} not available: {error}"
    record = planned
    try:
        record, reason = evaluate()
        if reason:
            return record, None, reason
        if budget_left is not None and record["bytes"] > budget_left:
            return record, None, f"over this run's max_bytes_per_run ({record['bytes']} > {budget_left} remaining)"
        # The scan can be long: decide only while the lease is still ours.
        try:
            system.renew(project, resource, token)
        except Unverifiable as error:
            return record, None, f"exclusion lock {resource} lost during the scan: {error}"
        path, retain = Path(record["path"]), retain_seconds(config, planned)
        ctx, now = Context(config, system), system.now()
        reason = recheck(ctx, planned, path, now, retain)
        if reason:
            return record, None, reason
        aside = path.with_name(f".{path.name}.retiring-{os.getpid()}")
        try:
            os.rename(path, aside)
        except OSError as error:
            return record, None, f"rename failed: {error}"
        # A writer that ignored the lock and entered or locked it between the
        # check and the rename is caught here; the output is renamed back.
        ctx, now = Context(config, system), system.now()
        reason = recheck(ctx, planned, aside, now, retain)
        if reason:
            if os.path.lexists(path):
                record["left_aside"] = str(aside)
                return record, None, reason + "; the original path was recreated, so the old tree is left aside, not deleted"
            os.rename(aside, path)
            return record, None, reason
        return record, aside, None
    finally:
        try:
            system.unlock(project, resource, token)
        except Unverifiable as error:
            record["unlock_error"] = str(error)


def evaluator(config, system, planned):
    """The in-place evaluation and scan of a planned candidate on a fresh
    snapshot, or the reason it no longer matches the configuration."""
    if planned["kind"] == "worktree-output":
        project = next((p for p in config.get("projects", []) if p["id"] == planned["project"]), None)
        if not project:
            return None, "ticket or worktree changed"
        if planned["relative"] not in config.get("worktree_outputs", DEFAULT_OUTPUTS):
            return None, "no longer allowlisted"

        def evaluate():
            ctx, now = Context(config, system), system.now()
            claim = next((c for c in ctx.claims.get(planned["project"], []) if c["key"] == planned["ticket"]), None)
            if not claim or claim.get("worktree") != planned["worktree"]:
                return planned, "ticket or worktree changed"
            return evaluate_output(ctx, project, claim, planned["relative"], now)
        return evaluate, None
    tree = next((t for t in config.get("yocto", []) if t["path"] == planned["tree"] and planned["relative"] in t.get("retire", [])), None)
    if not tree:
        return None, "no longer configured"

    def evaluate():
        ctx, now = Context(config, system), system.now()
        return evaluate_yocto(ctx, tree, planned["relative"], now)
    return evaluate, None


def apply(config, system, plan_data):
    """For each candidate: take the exclusion lock, re-evaluate and rescan it
    in place on a fresh snapshot and check the run's cumulative budget; then
    recheck, rename it aside, recheck once more, release the lock and delete
    it. Any refusal leaves the output in place."""
    if plan_data.get("kind") != "disk-cleanup-plan" or plan_data.get("schema_version") != SCHEMA:
        raise SystemExit("not a disk-cleanup plan")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise SystemExit("this platform's rmtree is not symlink-attack safe")
    roots = sorted({c["path"] for c in plan_data["candidates"]})
    before = filesystems([os.path.dirname(r) for r in roots] or ["/"], {})
    budget, spent, results = config.get("max_bytes_per_run"), 0, []
    for planned in plan_data["candidates"]:
        evaluate, reason = evaluator(config, system, planned)
        if reason:
            results.append({**planned, "result": "skipped", "reason": reason})
            continue
        record, aside, reason = retire(config, system, planned, evaluate, budget - spent if budget else None)
        if reason:
            results.append({**record, "result": "skipped", "reason": reason})
            continue
        spent += record["bytes"]
        errors = delete(aside)
        record["left_aside"] = str(aside) if aside.exists() else None
        remaining = measure(aside)["bytes"] if record["left_aside"] else 0
        # Blocks still linked from outside the tree are not reclaimed.
        results.append({**record, "result": "partial" if errors else "removed", "removed_allocated_bytes": record["bytes"] - remaining,
                        "freed_bytes": max(0, record["exclusive_bytes"] - remaining), "errors": errors[:10], "error_count": len(errors)})
    after = filesystems([b["path"] for b in before], {})
    return {"schema_version": SCHEMA, "kind": "disk-cleanup-result", "finished": system.now(), "results": results,
            "measured_freed_bytes": sum(r.get("freed_bytes", 0) for r in results),
            "removed_allocated_bytes": sum(r.get("removed_allocated_bytes", 0) for r in results),
            "filesystem_before": before, "filesystem_after": after,
            "filesystem_free_delta_bytes": sum(a["free_bytes"] for a in after) - sum(b["free_bytes"] for b in before),
            "note": "freed_bytes excludes blocks still hard-linked outside a removed tree; files still open elsewhere are freed "
                    "only when closed. Free-space deltas include unrelated concurrent writes. Cleanup establishes no build or test result."}


def audit(config, system):
    ctx = Context(config, system)
    outputs = config.get("worktree_outputs", DEFAULT_OUTPUTS)
    projects = {}
    unscannable = {}
    for project, claim in ctx.worktrees():
        path = Path(claim["worktree"])
        try:
            if not is_plain_dir(path):
                continue
        except OSError as error:
            unscannable[str(path)] = str(error)
            continue
        entry = projects.setdefault(project["id"], {})
        status = entry.setdefault(claim["status"], {"worktrees": 0, "bytes": 0, "regenerable_bytes": 0, "unreadable": 0})
        whole = measure(path)
        regenerable = 0
        for output in (path / o for o in outputs):
            try:
                if is_plain_dir(output):
                    regenerable += measure(output)["bytes"]
            except OSError as error:
                unscannable[str(output)] = str(error)
        status["worktrees"] += 1
        status["bytes"] += whole["bytes"]
        status["regenerable_bytes"] += regenerable
        status["unreadable"] += whole["unreadable"]
    yocto = []
    for tree in config.get("yocto", []) + [{"path": p, "report_only": True} for p in config.get("yocto_report_only", [])]:
        build, skipped = Path(tree["path"]), {}
        try:
            if not is_plain_dir(build):
                continue
        except OSError as error:
            unscannable[str(build)] = str(error)
            continue
        parts, unreadable = {}, {}
        for child in subdirs(build, skipped):
            for sub in (subdirs(child, skipped) if child.name.startswith("tmp") else [child]):
                name = str(sub.relative_to(build))
                measured = measure(sub)
                parts[name] = measured["bytes"]
                if measured["unreadable"]:
                    unreadable[name] = measured["unreadable"]
        try:
            idle_days = round((system.now() - yocto_activity(build)) / 86400, 1)
        except OSError as error:
            idle_days, skipped[str(build) + " (activity)"] = None, str(error)
        unscannable.update(skipped)
        yocto.append({"path": str(build), "configured_retire": tree.get("retire", []), "bytes": sum(parts.values()), "parts": parts,
                      "unreadable": unreadable, "unscannable": skipped, "complete": not skipped and not unreadable,
                      "idle_days": idle_days})
    extra = []
    for p in config.get("report_paths", []):
        try:
            if lstat(p) is None:
                continue
        except OSError as error:
            unscannable[p] = str(error)
            continue
        extra.append({"path": p, **{k: v for k, v in measure(Path(p)).items() if k != "protected"}})
    roots = [p["root"] for p in config.get("projects", [])] + [t["path"] for t in config.get("yocto", [])] + config.get("report_paths", [])
    return {"schema_version": SCHEMA, "kind": "disk-audit", "created": system.now(),
            "filesystems": filesystems(roots or ["/"], config.get("budget", {})),
            "worktrees": projects, "yocto": yocto, "report_paths": extra,
            "docker": {**system.docker_usage(), "note": "Docker's logical sizes overlap (shared layers, build cache) and are not physical "
                       "usage; its root directory is not scanned (permission-limited). Docker storage is never removed by this tool."},
            "unverifiable": ctx.errors, "unscannable": unscannable, "unreadable_processes": ctx.unreadable_processes,
            "uninspectable_process_uids": sorted(set(ctx.uninspectable_uids)),
            "note": "Bytes are allocated blocks with hard links counted once; entries that could not be read are counted, not sized; "
                    "paths that could not be listed or checked are under unscannable and their sizes are absent (lower bounds)."}


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
