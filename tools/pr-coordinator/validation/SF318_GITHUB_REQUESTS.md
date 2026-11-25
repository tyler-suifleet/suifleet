# SF-318 GitHub request and recovery fix

Observed cache invalidation classified `gh pr view` as a write, and all routine
writes evicted stable Project metadata. Both the shared provider and coordinator
could pause an entire account when only one API resource was exhausted. Full
PR scans also fetched four REST endpoints per PR before considering whether
local capacity or unfinished reviews prevented progress.

The portable engine now distinguishes reads, keeps Project metadata for five
minutes across ordinary writes, invalidates it on schema/unknown mutations,
coalesces discovery across processes and separates primary resource cooldowns
from shared secondary limits. Mutable authority and merge reads remain uncached.
Both cache and coordinator legacy pauses survive activation, and Retry-After is
honored in full. This reduces avoidable quota use; it cannot remove GitHub's
limits or guarantee that third-party throttling never occurs.

The coordinator uses local scheduling hints to avoid full snapshots when reviewer
slots are full, a PR has active work, or a source review is still unfinished.
Integration candidates, source-reviewed heads and infrastructure work retain
fresh discovery. The existing batch member validation and publication fences
still re-read exact ownership, head/base, feedback and CI evidence before merging.

Validation before integration:

- Initial worker: all 74 portable tests and all 891 coordinator tests passed.
- Final portable engine: all 76 tests passed, including cross-process cache
  coalescing, resource isolation, failed writes, authority-read freshness,
  legacy cooldown migration and full server retry intervals.
- Coordinator: five new scheduling/upgrade cases passed, plus 24 source-review
  tests and eight pipeline-lane tests. The scheduling cases show a ten-PR scan
  fetching only two full snapshots as its two reviewer slots fill, then zero
  on the next scan; integration performs zero full snapshots for ten PRs still
  waiting on source review. Selected heads still obtain fresh snapshots.

Install order: merge and verify atomic-ticketing first, then integrate and test
the actual SuiFleet main candidate. Install both committed runtimes with private
provenance receipts and restart only scheduling parents; preserve active model
workers. This follows the user's local-test infrastructure integration policy
(2026-11-01), without fabricating model approval or hosted-CI success. Installation
and post-activation observations are recorded in the PR/operator receipt.
