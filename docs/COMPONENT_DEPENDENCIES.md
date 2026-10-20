# Component dependencies

SF-012 adds device-side dependency resolution to catalog deployments
([COMPONENT_CATALOG.md](COMPONENT_CATALOG.md)). A deployment still names one
component version; the daemon resolves what that version needs, rejects
conflicts and cycles, and installs dependencies before their dependents.

## Declaring dependencies

Recipe schema `1.1` adds optional `componentDependencies`, keyed by catalog
component name. Schema `1.0` recipes are unchanged and cannot declare
dependencies. A daemon or frontend that only knows `1.0` rejects a `1.1` recipe
as unsupported before anything is applied.

```json
{
  "schemaVersion": "1.1",
  "componentName": "app",
  "componentVersion": "1.0.0",
  "artifactBlobId": "...",
  "artifactSha256": "...",
  "lifecycle": {},
  "configuration": {},
  "componentDependencies": {
    "lib-db": { "versionRequirement": "^3.0.0", "dependencyType": "HARD" },
    "metrics": { "versionRequirement": ">=1.0.0 <2.0.0", "dependencyType": "SOFT" }
  }
}
```

Requirements are space-separated comparators: `>=`, `>`, `<=`, `<`, `=`, `^`,
`~`, a bare full version meaning exact (as in npm, not Rust's caret default),
or `*`. Versions must be full SemVer 2.0.0 without build metadata. Partial
versions, comma lists and `||` alternatives are rejected. A prerelease
satisfies a requirement only if a comparator names the same core version with
a prerelease. The shared schema pattern and both validators enforce this
grammar; the cases are in `interfaces/recipe-cases.json`.

`dependencyType` defaults to `HARD`. A component cannot depend on itself.

## Resolution

Dependency candidates come only from the catalog that the deployed version is
bound to. The daemon reads the shared catalog's `name@version` index from the
object's BCS, then checks its type, shared ownership, ID and publisher. Each
candidate is authenticated as a frozen `ComponentVersion` of that catalog and
publisher. The platform manifest is selected as for a deployed version, and the
recipe bytes are checked against the manifest digest. A candidate without a
manifest for the device platform is unavailable. Candidates load lazily, from
the highest preferred version downward, and only when the resolver needs them.

- An installed confirmed version that satisfies every requirement is kept.
  Otherwise the highest satisfying available version is chosen. Ties use the
  full version string. The resolver backtracks across combinations within a
  fixed search bound.
- A hard dependency must be satisfied. A soft dependency that is not
  installed is omitted only when no available version satisfies all
  requirements on it. Omission never resolves a conflict or cycle. Once
  present, a soft dependency constrains versions like a hard one.
- Installed components outside the plan keep constraining the components
  that the plan changes, so a deployment cannot break an existing dependent.
- Cycles through either dependency type are rejected. The install order puts
  dependencies before dependents and breaks ties by component name.
- Schema-1 deployments have no catalog. Their dependencies can only be
  satisfied by already installed components.

The daemon rejects conflicts, cycles, unavailable hard dependencies, and
invalid candidates before it downloads any artifact or changes installed state.
It downloads and verifies every artifact in the plan before the first install.
A rejection is journaled as Failed and reported like other deployment
failures.

## Installed components

The journal (`state.json`) records `components` by name: version, deployment,
catalog and version IDs, recipe and artifact digests, declared dependencies,
and `confirmed`. Each install is recorded unconfirmed before it starts and
confirmed after it succeeds. With the firmware helper, the deployed component
is confirmed when the late result is Applied. An unconfirmed entry is never
reused, but its requirements still apply. This conservative state comes from a
crash, failure or indeterminate result; a later explicit deployment replaces it.

If a dependency install fails, later components in that deployment are not
installed, and the deployment is Failed. Dependencies installed earlier stay
installed and confirmed. There is no rollback or component supervisor.
Run/shutdown lifecycle and state coupling between hard and soft dependencies
remain unimplemented.

In `privileged-helper` mode, only the deployed component's artifact can go to
the root firmware helper. Dependencies must use recipe `install` commands, so
`allow_recipe_commands` must be enabled. Otherwise the plan is rejected before
any download.

## Offline candidates

```toml
[components]
offline_candidates = "never"   # default; or "cached"
cache_max_bytes = 536870912
```

With `never`, nothing is cached, and an unreachable catalog or Walrus fails the
deployment before installed state changes. With `cached`, the daemon keeps
authenticated data under `state_dir/components`: versions by catalog,
content-addressed recipes and artifacts. It uses verified local copies by
digest. If the catalog index is unreachable, it resolves from cached versions
of that catalog. Frozen versions cannot change, so a cached version equals the
on-chain object. Recipes and artifacts are re-verified by SHA-256 before use;
corrupt entries are discarded. A cached listing can be older than the live
catalog, so offline resolution may choose a lower version than online
resolution. Cached artifacts beyond `cache_max_bytes` are evicted oldest first,
except artifacts of installed versions. Artifact writes and verified cache hits
advance persistent logical modification times beyond the other entries, with
whole-second spacing so rapid accesses do not tie on coarse filesystems. These
ordering timestamps may be ahead of wall time; reopening the store preserves
their order. Copying a cached artifact out, and
adding one to the store, obey `[walrus] max_artifact_bytes` and
`min_free_bytes` like a download. A refused cached copy fails the deployment;
a refused cache write only skips caching. Protect the store like the journal.

## Validation

- `cargo test --locked`: requirement grammar, declared dependency validation,
  ordering, backtracking, conflicts, cycles, soft omission, installed
  reuse/protection, lazy loading, the search bound, and offline-store
  verification/eviction.
- `node scripts/test_protocol.cjs` (`make check-interfaces`) and the Rust
  recipe test: the same shared schema-1.1 recipe cases.
- `python3 scripts/test_dependency_ota.py`, part of `make test-ota`: the actual
  daemon and the SF-030 test updater against the simulator. It covers
  dependency-first installs, unavailable platform candidates, soft omission,
  conflicts with installed dependents and cycles before any artifact download,
  installed reuse, a failed dependency stopping its dependent, and cached
  offline candidates under `cached` but not `never`.

- `python3 scripts/test_catalog_localnet.py` (disposable Docker `--network none`,
  pinned Sui 1.81.1): after the compatible baseline upgrade, it registers
  schema-1.1 versions in a real shared catalog. The actual daemon reads the
  catalog's BCS index over gRPC, installs the highest matching dependency
  before its dependent, and records on-chain receipts. An unresolvable hard
  dependency is reported Failed without any artifact download or
  installed-state change.

## SF-012 recorded evidence (2026-10-15)

Claude `claude-opus-5-5` ran these checks in a `suifleet-dev:sf004` container
with `--network none`, using Sui `1.81.1-e04cf9aa915c`, Rust `1.94.0` and Node
`22.22.2`:

- `make check`: exit 0. It included Move tests (48 passed), interface
  generation and drift checks (30 shared recipe cases), `cargo test --locked`
  (74 passed), all `make test-ota` simulator suites including
  `test_dependency_ota.py`, component fixtures, frontend typecheck, and the
  production build.
- `python3 scripts/test_catalog_localnet.py`: exit 0. Every stage passed,
  including the new dependency stage.

These are local simulator and disposable-localnet results. Public-network catalogs, extension-wallet
authoring of schema-1.1 recipes, and hardware installs are not covered. The
frontend validates schema-1.1 recipes, but `/components` does not yet author
dependencies.
