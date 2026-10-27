# SF-166 implementation evidence — 2026-10-27

Owner: Codex, running model `gpt-6.1-sol`, session
`dispatch-f8f004e41c1d4ca28cd658a87d236461`, branch
`work/suifleet/sf-166-6bab6531`. Existing issue: #332. No dependencies.

The browser accepts ordered manifest publisher/aggregator lists, preserving legacy
single URLs. Publisher 5xx/network/timeout errors try unchanged protected bytes at
the next publisher; cancellation, rejected requests and malformed successes stop.
Operator-added session overrides also fall through on rejected or malformed
responses and can be listed/removed in Settings. Uploads record their serving publisher. Read probes and archive readers fail over
across configured aggregators, recording the serving endpoint without weakening
archive authentication. Session overrides are validated, stored in tab session
storage, and available from Settings without a rebuild. Fleet-paid storage is
selected automatically when available and never retried through public publishing.
Raw publisher failures are escaped under Technical details, with an endpoint and
action in the visible message. Release upgrades/recovery preserve ordered lists.

## Verification

- `npx --yes pnpm@11.0.8 install --frozen-lockfile` in `dapp`: passed.
- `make dapp-typecheck` in a disposable `suifleet-dev:latest` container with
  `--network none`, this worktree mounted at `/workspace`, `INSIDE_DEV_CONTAINER=1`
  and `pnpm_config_verify_deps_before_run=false`: passed, including the offline
  testnet/manifest/frontend regressions and TypeScript. One existing environment
  dependent test is skipped by the suite. Dependencies were installed with the
  pinned lockfile; the pnpm option prevents cross-host/container reinstall prompts.
- `node scripts/test_walrus_publishers.cjs`: passed on final source/tests; fetch/XHR
  retry order, encryption reuse, exhausted/nonretryable/malformed responses,
  escaped error rendering, read failover and uncertainty, session persistence,
  rejected URLs, cancellation and failed session saves.
- `node scripts/test_upload_progress.cjs`: passed.
- `node scripts/test_fleet_bulk.cjs` and `node scripts/test_fleet_recovery.cjs`:
  passed after archive-read integration.
- `npx --yes pnpm@11.0.8 typecheck` in `dapp`: passed on final browser source.
- `INSIDE_DEV_CONTAINER=1 make dapp-build` with a temporary PATH wrapper selecting
  pnpm 11.0.8: passed on final browser source; static routes exported.
- `INSIDE_DEV_CONTAINER=1 make test-publish`: passed on final Python source:
  37 publisher tests and 27 release-plan tests, including list preservation when
  the list differs from the legacy URL.
- `git diff --check`: passed.

Initial host Make runs could not find the stopped Compose service, pnpm or Sui.
The first container run reached all frontend regressions but its final pnpm command
requested a cross-environment reinstall. The successful commands above resolve
those tooling issues; no failed test was suppressed or treated as acceptance.

## Live secondary publisher

[Machine-readable receipt](sf166-secondary-publisher.json). Direct gRPC preflight
confirmed Sui Testnet `4c78adac`, epoch 1265. The real `uploadArtifact` implementation
tried h2o-nodes then succeeded at n1stake. One-epoch sponsored publication reported
563,787 FROST, with **zero wallet SUI spend and no wallet signature**. Independent
gRPC reads verified the Blob owner and certification transaction successful effects;
public aggregator readback matched the original SHA-256. The global testnet lock
was held during publication and released; coordination is recorded in #11.
No application package/UpgradeCap or existing app objects were changed.

## Remaining gates and limits

Hosted exact-head/current-base CI and independent review are pending at handoff.
A session override affects this browser tab, not fleet-wide chain/device settings.
The public publisher can charge its own storage budget; the reported FROST is not
an invoice to the operator. The stored test blob expires at Walrus epoch 564
(distinct from the Sui epoch). No final production-provider, real-wallet, physical
Pi or mainnet acceptance was run; those remain SF-031/#32 and SF-087/#159 under
the authorized implementation/acceptance split. No migration is required.

## PR #343 feedback repair — 2026-10-27

Addressed all six independent review findings. Unavailable selected fleet storage
stays checked with its reason and permits an explicit opt-out. Component publishing
and Greengrass import share the same automatic fleet-paid selection. Session
publishers are listed/removable, and failed session overrides cannot strand the
manifest route after a reload. Caller-provided read deadlines cover body reads
without a shorter imposed deadline; cancellation retains its abort reason. Browser
reads retain the legacy aggregator after ordered mirrors, whose HTTPS/CORS needs
are documented. Publisher failures show a plain failure category (HTTP status for
rejected requests) once, with escaped raw diagnostics under Technical details.

Verification on the repaired source:

- `docker run --rm --network none -v "$PWD:/workspace" -w /workspace -e INSIDE_DEV_CONTAINER=1 -e pnpm_config_verify_deps_before_run=false suifleet-dev:latest make dapp-typecheck`: exit 0, including offline testnet/manifest/frontend/browser regressions and TypeScript; one existing environment-dependent test skipped.
- Same container/options with `make dapp-build`: exit 0; production compilation, TypeScript and all 16 static pages generated.
- Host `node scripts/test_walrus_publishers.cjs`: exit 0, including persisted bad override fallback/removal, configured-but-unavailable selection/opt-out, caller signal preservation, final-endpoint abort and legacy read fallback.
- Host `node scripts/test_upload_progress.cjs`, `node scripts/test_fleet_bulk.cjs`, `node scripts/test_fleet_recovery.cjs`, `node scripts/test_greengrass_compat.cjs`: all exit 0. The last checks 83 AWS fixtures and 47 managed-dependency fixtures.
- `git diff --check`: exit 0.

An initial extra command named nonexistent `scripts/test_greengrass_recipe.cjs`
and failed with MODULE_NOT_FOUND; the actual compatibility test above passed.
`git fetch origin main` followed by `git merge --no-edit origin/main` reported
already up to date, and ancestry was verified against base
`88c7079c386667f1dc83ca8f4b9dcedbcc06cb8e`.
No new live transactions, hardware operations or deployments were performed.
The earlier real secondary-publisher receipt above remains historical evidence,
not a new exact-head run. Hosted CI/automatic testnet and fresh independent review
remain coordinator gates after push. External acceptance remains deferred in
SF-031/#32 and SF-087/#159; no migration is required.

## Current-base refresh after second review — 2026-10-27

The second independent review found no actionable source defects and confirmed
all six previous findings resolved. Its outstanding gates were hosted CI/testnet
and a fresh evidence review. Merged current main
`af805cc1736dd12849fa396d047a3ad29f99c5e0` without conflicts; upstream changes
were limited to backlog/publication tracking records. No additional source change
was needed.

Re-ran the exact network-disabled container command above with
`make dapp-typecheck dapp-build`: exit 0, including TypeScript, frontend/browser
regressions and all 16 production static pages; one existing environment-dependent
skip. Host `node scripts/test_walrus_publishers.cjs` and `git diff --check` also
passed. No new live publication or on-chain verification was run; the earlier
secondary-publisher receipt remains historical evidence.

Known nonblocking review limitations remain: a hanging first aggregator may consume
the caller deadline before mirrors are tried; an override duplicating a configured
publisher retains override retry behavior; overrides are per-tab. No migration.
Hosted exact-head/current-base CI and automatic testnet remain pending coordinator
verification, followed by independent review. They were not polled by this worker.
Deferred external acceptance stays in SF-031/#32 and SF-087/#159, explicitly unrun.
