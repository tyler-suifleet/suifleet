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
| `yocto` | Per tree: bytes per top-level directory and per `tmp*/` subdirectory, unreadable entries per part, `unscannable` directories that could not be listed, `complete`, idle days (`null` when activity markers cannot be read), and which entries are configured for retirement |
| `docker` | `docker system df` logical sizes. These overlap (shared layers, build cache) and are not physical usage. Docker's root directory is not scanned (it is root-owned) |
| `report_paths` | Any other paths to size, report-only |

Bytes are allocated blocks, with hard links counted once, never following symlinks. Plans also report `exclusive_bytes` per candidate (and `candidate_exclusive_bytes`): the part deleting it would free, excluding files that keep a hard link outside it. Entries the scan cannot read are counted in `unreadable`, never silently treated as empty. A worktree, Yocto build or `tmp*` directory, or report path that cannot be listed or checked is recorded with its error under `unscannable` (per tree and at the top level) and the rest of the audit is still produced; sizes are then lower bounds. In plans, a configured path that cannot be checked is excluded with `cannot inspect path`, and an unreadable BitBake activity marker refuses the tree (`tree activity unverifiable`) instead of treating it as idle. `unreadable_processes` counts processes whose working directory, root, descriptors or maps could not all be read, and `uninspectable_process_uids` lists their owners. A process that exits or closes a descriptor mid-scan is not counted; any other failure, including one denied descriptor, is.

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

Each planned candidate goes through these steps. If any check fails, the candidate is skipped with the reason and left in place:
1. **Exclusion lock.** Take the coordinator lock `build:TICKET` (for example `build:SF-200`; for Yocto, the tree's `lock`, default `build:<tree parent directory>`) before anything else is checked. A live lock held by anyone else, or an unreachable coordinator, refuses. While cleanup holds it, the coordinator refuses that lock to any worker, so **a worker that builds in a `done` worktree must take `build:TICKET` first**. A worker that wrote and released the lock before cleanup took it has finished, and its changes are visible to the scan in step 2.
2. **Recheck and rescan in place**, with the lock held. Re-evaluate the candidate against a fresh snapshot of claims, locks, processes and containers, plus the Git, symlink and idle checks, and scan it for protected files, unreadable entries, recent modification and its current size. The output stays at its path, intact and usable, for the whole scan.
3. **Budget.** The size measured under the lock is added to the run's total, and the candidate is refused if that would exceed the current `max_bytes_per_run`. A stale plan, a reduced ceiling or a grown output cannot exceed it. The lease is then renewed; if it was lost during a long scan, the candidate is refused.
4. **Final check** on another fresh snapshot, taken after the scan with the lock held: a claim reopened, any other lock naming it (expired ones included), a `bitbake.lock` or new Git/Yocto activity, or a process with its working directory, a descriptor or a mapping inside the worktree, all refuse.
5. **Rename aside**, atomically within its parent (`.NAME.retiring-PID`), then **check once more**. A process or lock that appeared between the final check and the rename (a writer that ignored the lock) is caught here, and the tree is renamed straight back. If the original path was recreated meanwhile, the old tree is left aside (`left_aside`), not deleted and not merged back.
6. **Release the lock and delete** the aside tree with Python's symlink-attack-safe `rmtree`. Apply refuses to run on a platform without that guarantee. A worker locking `build:TICKET` from now on gets a fresh output; nothing can reach the old tree by its path.

The exclusion is cooperative, like the coordinator itself. Workers that take `build:TICKET` are excluded outright. A worker using another lock name, or none, is detected by the checks in steps 2, 4 and 5. Only one that opens the output by path in the moment between the rename and the check after it would briefly find it missing before it is restored. A worker that skips the lock and exits between two snapshots is not detected, but it never shares the tree being deleted.

Each candidate's outcome is reported:
- `removed`, with `removed_allocated_bytes` (blocks unlinked) and `freed_bytes` (those actually reclaimed: blocks still hard-linked outside the tree are excluded);
- `partial`, with errors and the aside path left for inspection;
- `skipped`, with the reason;
- `failed`.

Other items continue after a failure. The result records filesystem free space before and after. The free-space delta includes unrelated concurrent writes, so `measured_freed_bytes` (the sum of `freed_bytes`) is the per-item figure. Files a process still holds open are reclaimed only when closed; the process checks refuse such candidates. Cleanup establishes no build or test result, and does not complete deferred target acceptance.

## Evidence

| Command | Covers |
|---|---|
| `make test-disk-cleanup` | Offline, real Git worktrees in a temporary directory with a fake coordinator, process table and Docker. Dry run selects only done idle outputs and mutates nothing. Review, in-progress, recent work. Locks (including expired), processes inside the output or worktree, container mounts. The coordinator or Docker unavailable means nothing is eligible. Symlinked outputs and path components, `..` and absolute entries. Tracked, unignored and protected content. The run budget. Uninspectable processes fail closed, including a single denied descriptor in the real `/proc` scan, and an accepted uid never covers the candidate owner. Apply: removal with freed bytes, reopened tickets and a build started mid-run skipped. A lock, a process or a reopened claim appearing during the apply-time scan refuses the candidate without moving it, and the output stays readable throughout. A worker locking `build:TICKET` during the scan or just before the rename is refused by the coordinator while cleanup holds the lock, and gets it after release. A worker already holding the lock, or an unreachable coordinator, refuses cleanup's lock before any scan. A worker that locks, writes a key and a large output and releases just before cleanup's lock is seen by the scan: the key and recency refuse, and the fresh size is held to the budget. A lease lost during the scan refuses without moving the output. A differently named lock or a process appearing just before the rename is caught after it and the output is renamed back intact. A path recreated after the rename keeps the old tree aside. A file hard-linked outside the candidate counts in `removed_allocated_bytes` but not `freed_bytes`. The cumulative apply-time budget refuses a reduced ceiling and a grown candidate. Partial failure reported while others continue, forged or unconfigured plan entries refused. Yocto: unconfigured trees untouched, only `tmp*/work` retired, deploy, configuration and downloads kept, invalid retire entries refused, `bitbake.lock`, locks, processes and recent activity block. An unreadable subtree containing a key blocks both plan and apply, is restored intact and is reported by the audit. An unlistable build root or `tmp` directory, worktree root or report path is reported under `unscannable` without aborting the audit, and excludes the candidate in plan and apply; an unreadable activity marker refuses the tree. Audit: categories without mutation, unreadable entries reported |

A first read-only run on the build host (2026-10-16) found 891 GiB used of 1007 GiB (`warn`):
- 45 done suifleet worktrees holding 112.5 GiB, of which 112.2 GiB was allowlisted outputs;
- four Yocto trees of 20–95 GiB each, mostly `tmp*/work`;
- Docker's logical 50.8 GiB of images and 53.4 GiB of build cache.

With `retain_days` 1, the dry run found 54 eligible outputs (47.9 GiB). 23 more were held by expired, unbroken build locks.
