# Component versions and platform manifests

SF-011 adds a publisher catalog, immutable component versions, and device-side
platform selection. The wallet `/components` page creates catalogs, uploads
artifacts and authenticated schema-1.0 recipes, registers versions, and discovers
versions for deployment through the existing target selector. Direct verified
OTA remains available at `/firmware`.

A shared catalog belongs to its creating publisher address. Only that address
can register versions. Names contain 1–128 ASCII letters, digits, `.`, `_`, or
`-`; `@` is excluded so the index `name@fullVersion` is unambiguous. Each full
version string is unique within a catalog. Registered `ComponentVersion` objects
are frozen and have no mutation API. Catalogs are explicitly created: an upgrade
does not rerun an initializer.

Versions follow [SemVer 2.0.0](https://semver.org/spec/v2.0.0.html), with a
128-byte limit and each core number bounded to u64. Numeric prerelease
identifiers reject leading zeros. Build metadata participates in registration
identity; discovery sorts by SemVer precedence and uses the full version string
as a deterministic tie break. Component names are sorted before versions.

Each version contains 1–16 ordered manifests. All five manifest vectors must
have equal lengths. Duplicate OS/architecture pairs, empty or unsafe blob IDs,
and hashes other than 64 lowercase hexadecimal characters are rejected. Every
manifest is validated, including entries that are not selected. Each manifest
binds exact recipe bytes and artifact bytes with separate SHA-256 digests.

Selection uses the first manifest whose OS and architecture match the daemon's
Rust `std::env::consts::OS` and `ARCH`, or whose corresponding field is `*`.
Supported catalog values are OS `linux`/`*`, architecture
`x86_64`/`aarch64`/`arm`/`x86`/`*`. Rust `arm` does not imply armv7; `x86` is
32-bit i686 ([LINUX_X86.md](LINUX_X86.md)), never an alias of `x86_64`. Put specific entries
before wildcards if they should take precedence. The selection order follows
the first-matching-platform principle described in the
[AWS recipe reference](https://docs.aws.amazon.com/greengrass/v2/developerguide/component-recipe-reference.html).
This is a deliberately bounded platform vocabulary; it does not implement AWS
platform regular expressions, AWS recipe formats, or full Greengrass runtime
interoperability (SF-038).

A catalog deployment retains the original `DeploymentRecord` layout and event,
uses object schema 2, and attaches a typed `CatalogBindingKey -> CatalogBinding`
dynamic field naming the immutable version object. Its global recipe ID and
artifact digest are empty by design. Each targeted device resolves that binding,
authenticates the version's type origin and immutable owner, verifies component
identity and all manifest data, then selects locally before creating an artifact
directory, saving updater intent, or downloading anything. A mixed group receives
one deployment ID and independently selects platform-specific bytes. Recipe
schema remains 1.0; install command opt-in is unchanged, and run recipes become
supervised components (SF-014, docs/OPERATIONS.md). Failures are journaled as Failed and advance the discovery cursor;
report retries survive restart without repeating an update.

Manifest schema/protocol remain 1. The reviewed interface metadata advertises
deployment schemas `[1,2]`. Catalog support adds an optional, all-or-none group of
six type origins. Legacy manifests without the group remain valid for schema-1
OTA; schema-2 resolution requires it. All advertised origins are verified against
the live call package's origin table. Never substitute the current call package
for a missing origin. Old types retain their introduction package after an
upgrade; new catalog types originate at the adding upgrade.

The frontend is version 0.1.0 and daemon 0.2.0. The resolver sets these as minimum
client versions for catalog-enabled packages. Schema-1 fixtures continue to
admit the new clients. There are no unrelated dependency upgrades; Rust adds the
already-locked `semver` 1.0.28 as a direct dependency.

Use `component_catalog::delete_deployment` for schema-2 cleanup. It checks the
creator, removes the catalog binding and delegates existing target/receipt
cleanup. Current `deployment_manager::delete_deployment` rejects schema 2 until
that binding is removed. Historical packages remain callable and can still
delete their original-layout records without understanding the binding, leaving
an unreachable dynamic field. This does not permit changing a frozen version or
forging an authoritative receipt. Do not assume old package entry points are
disabled or that deletion implements cancellation.

Recipe schema 1.1 dependencies, resolution and install ordering for catalog
deployments are documented in [COMPONENT_DEPENDENCIES.md](COMPONENT_DEPENDENCIES.md).

## Validation

`make check` includes contract tests, shared TypeScript/Rust selector cases in
`interfaces/component-cases.json`, the actual daemon catalog simulator, original
OTA/restart/component suites, manifest proofs, generated ABI checks and frontend
typecheck/build. `python3 scripts/test_catalog_localnet.py` requires a disposable
Docker container with `--network none`, pinned Sui 1.81.1, frontend dependencies,
and a built debug daemon. The worktree's Git common directory must also be
mounted at its absolute path so it can read baseline commit `2d2a01e`.

The localnet runner checks generated SDK catalog registration/discovery/selection
and cleanup, then publishes baseline contracts, creates existing fleet objects,
upgrades compatibly, verifies old and new origins, explicitly creates the new
catalog, proves frozen ownership, and runs the real daemon through schema-2
selection and on-chain status receipts. It also retains the original daemon
legacy direct/group/offline/cursor tests. Local chain and HTTP storage evidence
does not establish public-network, extension-wallet, Walrus certification,
Yocto-image, or hardware firmware safety acceptance.

## SF-011 recorded evidence (2026-10-14 America/Los_Angeles)

Pinned image `suifleet-dev:sf004`: Sui `1.81.1-e04cf9aa915c`, Rust/Cargo
`1.94.0`, Node `22.22.2`, pnpm `11.0.8`, Python `3.10.12`.

- `make ci`: passed all steps, including generated ABI, manifest/wallet/query
  regressions and final `git diff --check`.
- `sui move test --path contracts`: 31 passed, 0 failed.
- `cargo test --locked --manifest-path suifleet-edge/Cargo.toml`: 54 passed,
  0 failed. `cargo build --locked --manifest-path suifleet-edge/Cargo.toml
  --bins --examples` passed.
- `node scripts/test_component_catalog.cjs`: 17 fixed catalog/selector vectors
  plus SemVer discovery ordering passed; the same cases passed in Rust.
- `python3 scripts/test_catalog_ota.py`: actual daemon schema-2 selection,
  two different authenticated manifests, invalid unselected hash, unsupported
  platform, mutable/wrong-type versions, missing origins, no-download failures,
  cursor progress, restart deduplication and durable reports passed.
- `python3 scripts/test_ota.py`, `python3 scripts/test_ota.py --firmware-helper`,
  `python3 scripts/test_firmware_restart.py`, and
  `python3 scripts/test_component_ota.py`: passed. Component fixture tests ran
  14 cases with 5 BusyBox-only cases skipped because BusyBox was absent.
- `(cd dapp && pnpm typecheck && pnpm build)`: passed; eight static pages
  generated, including `/components`.
- `python3 scripts/test_catalog_localnet.py`: passed in Docker `--network none`.
  The successful disposable run reported chain `9d51d1ff`. It exercised actual
  SDK transactions/effects and BCS reads, then actual baseline upgrade,
  immutable ownership/origins, schema-2 direct/group application and on-chain
  receipts, unsupported-platform no-download failure followed by success,
  retained schema-1 health/offline/cursor cases and old entry points.

The runtime execution evidence is for x86_64. aarch64/arm selection is covered
by shared independent vectors and SDK reads; no physical ARM device ran an
update. The initial harness attempts exposed a default-public-endpoint wallet
setup and missing linked Git-directory mount; those were corrected before the
successful network-none run. No failed acceptance was counted as a pass.
