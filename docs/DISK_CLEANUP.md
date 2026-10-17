# Disk audit and inactive-output cleanup (SF-080)

`scripts/disk_cleanup.py` reports where disk space goes on the shared WSL build host. It removes only regenerable build outputs of completed, inactive work. It is standard-library Python and runs on the host, not in the dev container, because it reads the coordinator and Docker.

```bash
cp docs/disk-cleanup.example.json ~/.config/suifleet/disk-cleanup.json   # then edit
python3 scripts/disk_cleanup.py audit                      > audit.json    # read-only
python3 scripts/disk_cleanup.py plan --out plan.json                       # read-only dry run
python3 scripts/disk_cleanup.py apply plan.json --yes      > result.json   # deletes
```

`--config PATH` selects another configuration. `plan --out` refuses to overwrite. `apply` without `--yes` refuses, and exits 1 if any removal failed or was partial.

## Audit

The audit is JSON, one section per category:

| Section | Contents |
|---|---|
| `filesystems` | Size, free bytes, used fraction and pressure (`ok`, `warn`, `critical`) against `budget`, once per filesystem |
| `worktrees` | Per project and coordinator status: worktree count, bytes, regenerable bytes (the allowlisted outputs) and unreadable entries |
| `yocto` | Per tree: bytes per top-level directory and per `tmp*/` subdirectory, unreadable entries per part, idle days, and which entries are configured for retirement |
| `docker` | `docker system df` logical sizes. These overlap (shared layers, build cache) and are not physical usage. Docker's root directory is not scanned (it is root-owned) |
| `report_paths` | Any other paths to size, report-only |

Bytes are allocated blocks, with hard links counted once, never following symlinks. Entries the scan cannot read are counted in `unreadable`, never silently treated as empty. `unreadable_processes` counts processes whose working directory, root, descriptors or maps could not all be read, and `uninspectable_process_uids` lists their owners. A process that exits or closes a descriptor mid-scan is not counted; any other failure, including one denied descriptor, is.

## What may be removed

**A worktree output is eligible only when all of these hold:**
1. Its relative path is in `worktree_outputs`: plain relative paths, no `..` and not absolute.
2. The coordinator reports the ticket `done`, with no publication pending. Review, blocked and in-progress work keeps its outputs, because fix workers build there.
3. The worktree's Git metadata and the output's contents are both older than `retain_days`.
4. No component of the path is a symlink, and its real path stays inside the worktree.
5. Git ignores it and it contains no tracked file.
6. No protected file is inside: keys, PEM, keystores, wallet `client.yaml`, `.env*`, journals, `config.toml`, enrollment or signer TOML, `deployment.json`, `github-state.json`, SQLite databases, mnemonics or recovery files. Nothing is unreadable inside.
7. No coordinator resource lock names the ticket or the worktree in its resource, ticket, purpose or session. **Expired locks still count:** break them with `ticket break-lock` only after checking that the holder has stopped.
8. No process has its working directory, root, an open file or a mapped file inside the worktree. **Uninspectable processes fail closed:** if any process cannot be fully inspected, nothing is eligible unless its uid is listed in `uninspectable_uids_accepted`. Run as the owner of the worktrees (or root) so their processes are visible, and accept only uids you know cannot be building there (for example a web server). Acceptance never covers the uid that owns the candidate: an uninspectable process of that uid always blocks it.
9. No running container mounts the output, the worktree or a parent of either.

**A Yocto entry is eligible only if a tree is listed under `yocto`** with an explicit `retire` list. Each entry must be `tmp*/work` or `tmp*/work-shared`, which are BitBake's per-recipe build directories and are rebuilt from sstate and downloads. The same symlink, protected-file, process and container checks apply. In addition:
- the tree is idle for its `retain_days`, judged by the cooker log and the `tmp*/log`, `stamps` and `deploy` directories;
- there is no `bitbake.lock`;
- no lock mentions the tree, `yocto` or `bitbake`;
- no `preserve` entry lies inside the candidate;
- nothing inside is unreadable (an unreadable subtree could hide protected material) or modified within `retain_days`.

Images, deploy directories, configuration, `downloads`, `sstate-cache`, manifests and evidence are never candidates. Trees under `yocto_report_only` are only measured.

**Never removed:** Docker images, containers, volumes and build cache. FPGA installations, arbitrary downloads, source, Git history, keys, journals, enrollment configuration and deployment state. Anything not allowlisted.

If the coordinator, `ticket locks`, `docker ps`/`inspect` or a non-accepted process cannot be read, nothing is eligible (fail closed). `max_bytes_per_run` bounds a single plan, taking the largest outputs first, and is enforced again during apply.

## Apply

Each planned candidate goes through these steps; if any check fails, the candidate is renamed back and skipped with the reason:
1. **Recheck.** Re-evaluate the candidate against a fresh snapshot of claims, locks, processes and containers, plus the Git, symlink and idle checks. A ticket reopened or a build started since the plan is skipped.
2. **Rename aside**, atomically within its parent (`.NAME.retiring-PID`). From here, nothing can reach the old tree by its path: a build that starts now creates a fresh output.
3. **Scan the aside tree**: protected files, unreadable entries, recent modification and its current size.
4. **Recheck again** with another fresh snapshot, taken after the scan. A claim reopened, a lock acquired (expired ones included), a `bitbake.lock` or new Git/Yocto activity, or a process with its working directory, a descriptor or a mapping inside the worktree or aside tree, all abort. Because the tree was already renamed, a process that entered before the rename still holds it and shows up here, and anything starting later uses the fresh path.
5. **Budget.** The freshly measured size is added to the run's total, and the candidate is refused if that would exceed the current `max_bytes_per_run`. A stale plan, a reduced ceiling or a grown output cannot exceed it.
6. **Delete** with Python's symlink-attack-safe `rmtree`. Apply refuses to run on a platform without that guarantee.

If the original path was recreated while the tree was aside, the old tree is left aside (reported in `left_aside`), not deleted and not merged back.

The checks are cooperative, like the coordinator itself: a worker that builds in a `done` worktree without claiming it or taking a lock, and exits between the two snapshots, is not detected. Such a worker gets a fresh output after the rename, but never shares the tree being deleted.

Each candidate's outcome is reported:
- `removed`, with `freed_bytes`;
- `partial`, with errors and the aside path left for inspection;
- `skipped`, with the reason;
- `failed`.

Other items continue after a failure. The result records filesystem free space before and after. The free-space delta includes unrelated concurrent writes, so `measured_freed_bytes` is the per-item figure. Cleanup establishes no build or test result, and does not complete deferred target acceptance.

## Evidence

| Command | Covers |
|---|---|
| `make test-disk-cleanup` | Offline, real Git worktrees in a temporary directory with a fake coordinator, process table and Docker. Dry run selects only done idle outputs and mutates nothing. Review, in-progress, recent work. Locks (including expired), processes inside the output or worktree, container mounts. The coordinator or Docker unavailable means nothing is eligible. Symlinked outputs and path components, `..` and absolute entries. Tracked, unignored and protected content. The run budget. Uninspectable processes fail closed, including a single denied descriptor in the real `/proc` scan, and an accepted uid never covers the candidate owner. Apply: removal with freed bytes, reopened tickets and a build started mid-run skipped. A lock, a process or a reopened claim appearing during the final scan of the renamed tree aborts it and restores the output intact. A path recreated meanwhile keeps the old tree aside. The cumulative apply-time budget refuses a reduced ceiling and a grown candidate. Partial failure reported while others continue, forged or unconfigured plan entries refused. Yocto: unconfigured trees untouched, only `tmp*/work` retired, deploy, configuration and downloads kept, invalid retire entries refused, `bitbake.lock`, locks, processes and recent activity block. An unreadable subtree containing a key blocks both plan and apply, is restored intact and is reported by the audit. Audit: categories without mutation, unreadable entries reported |

A first read-only run on the build host (2026-10-16) found 891 GiB used of 1007 GiB (`warn`):
- 45 done suifleet worktrees holding 112.5 GiB, of which 112.2 GiB was allowlisted outputs;
- four Yocto trees of 20–95 GiB each, mostly `tmp*/work`;
- Docker's logical 50.8 GiB of images and 53.4 GiB of build cache.

With `retain_days` 1, the dry run found 54 eligible outputs (47.9 GiB). 23 more were held by expired, unbroken build locks.
