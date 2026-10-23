# Dashboard: device services, logs and rollouts (SF-084)

The dApp shows device service health, alerts, archive log search and staged
rollout progress without an HTTP indexer. Every value comes from the chain or
from a device signature verified in the browser. Data that cannot be verified,
is out of date, or is never published is shown as **unavailable** with the
reason. The dashboard never shows stale or partial values in its place.

## Device services (`/services/?device=ID`)

The page needs the direct-chain fleet data configuration (`NEXT_PUBLIC_FLEET_DATA`;
see [serverless fleet data](SERVERLESS_FLEET_DATA.md)). It lists devices from the
fleet's on-chain index. The selected device's newest health summary is read
through `fetchChainHealth`, which checks the following before any value is used:

- the writer object and its anchor;
- the device's registry identity;
- the personal-message signature;
- the services section shape.

`lib/device-services.ts` turns that result into a view:

- **Values are shown** only for a `healthy` or `degraded` report that is not
  `offline` or `delayed` and that has a version-1 or version-2 services section.
- **Everything else is unavailable**, with the reason: unverified (for example a
  forged signature), unreported, revoked, offline, delayed (buffered), a chain
  outage, a malformed section, or a report without a services section.
- **A failed or stalled refresh** makes the earlier result unavailable
  (`currentHealth`). The page never keeps it on screen.

| Section | Shown from the signed report | Not available, and why |
|---|---|---|
| Shadows | Number of shadows, refused delta notifications. Per synchronized shadow: conflicts, consecutive failures, last success, error category | Desired, reported and delta **documents** are never in public health. The page links to them on the archives page, where only the owner and support accounts can decrypt them ([below](#shadow-documents)) |
| Streams | Bytes, maximum size, fill, messages, overwrite policy, rejected appends. Oldest and newest retained sequence numbers. Export checkpoint (the last acknowledged sequence), messages behind, batches, losses, failures, last success, error category | Sequence positions and the checkpoint, from a daemon that reports services version 1. Message times are not reported |
| Messaging | Pub/sub subscriptions, queued, refused and dropped messages, per-principal queues | — |
| Bridge | Connection state and duration, connects, refused and rejected messages, error category | — |
| Alerts | This device's active SF-020 alerts from `chain-alerts.ts`, with links to the deployment and the device's logs | If any fleet device's health cannot be verified, every device's alerts are unavailable rather than partial |

Alerts on the dashboard link to the device's services page.

## Logs and diagnostic bundles (`/data/`)

- **Who can open archives.** Opening, searching and requesting diagnostics need
  read authority: the account holds the fleet `BudgetOwnerCap`, or the fleet's
  `support` list names it. The page checks this on chain before showing the
  controls. Any other account sees that it is not authorized and gets no
  archive actions.
- **How an archive is opened.** It is fetched and verified with
  `fetchVerifiedArchive`, using a wallet-signed Seal session limited to five
  minutes. Log search is a literal substring search (never a regular
  expression), optionally filtered by source and case. It is bounded to
  256-byte queries and 1,000 shown matches.
- **Where plaintext lives.** Decrypted records exist only in page memory.
  Nothing is written to web storage or React Query caches. They are discarded
  when you close the archive, change the account, network or device, or when
  the Seal session expires.
- **How access is recorded.** Each open (or refused open) is added to an
  in-memory access record for the session: time, account, authority (owner or
  support), device, archive index and kind. This record is not durable and is
  never sent anywhere. Each Seal key request carries the signed session.
  Diagnostic requests are durable on-chain records. The session list is not a
  durable or independently verifiable audit trail of reads, and no on-chain
  read audit exists.

## Shadow documents

A device with `[shadows]` and encrypted fleet archives puts its current shadow
documents in every archive (schema 2, see
[serverless fleet data](SERVERLESS_FLEET_DATA.md)). Any change to them produces
a new archive at the routine batch cadence. On `/data/`, the owner or a support
account opens **Open current shadow documents** (the newest archive) or any
listed archive. Each shadow shows its name, version and update time, and its
desired, reported and delta documents as text. The checks on every document:

- **Signed and bound.** The device's archive signature must verify, and the
  archive must belong to this chain, registry, fleet, device and anchored
  sequence. Seal decryption must pass the owner/support policy, revocation, key
  epoch and the live storage record (`fetchVerifiedArchive`).
- **Strictly parsed.** `lib/shadow-documents.ts` refuses unknown fields,
  unsorted or duplicate names, nulls, too much depth, and a delta that does not
  follow from desired and reported. The shared cases with the daemon are in
  `interfaces/compat/archive-shadow-cases.json`.
- **Never shown rounded.** A document holding an integer beyond the exact range
  of browser numbers is shown as unavailable.
- **Labelled by age.** The newest archive is labelled *current* with its signing
  time; changes made since then arrive with the device's next archive. An older
  archive is labelled *history*. A schema-1 archive has no documents and says so.

The decrypted documents follow the same rules as log records: they stay in page
memory and are discarded with the archive or when the session expires.

## Staged rollouts (`/rollouts/`)

The overview lists every schema-3 rollout the connected account created, with:

- its state;
- the current stage;
- how many members have been granted;
- applied and failed outcomes;
- the failure threshold, and whether it has been reached.

Selecting a rollout shows:

- each cohort's members, grants, members in flight, applied, failed, skipped and
  waiting;
- the remaining bake time;
- the full policy;
- the members' grant times;
- the newest audited `RolloutDecision` events.

An aborted rollout shows the audited abort reason.

`lib/rollout-progress.ts` checks the on-chain `Rollout` record against the
contract's invariants before displaying anything:

- the member vectors have the same length;
- cohorts are ordered and each meets its minimum sample;
- there are no duplicate members;
- no outcome exists without a grant;
- no skipped member was admitted;
- no grant is ahead of the current cohort;
- every state, outcome and policy value is within its bounds.

Decision events must have unique revisions no newer than the record. A record or
history that breaks any of these rules is reported as unavailable, never shown
in part.

## Validation

| Command | Checks |
|---|---|
| `node scripts/test_dashboard_views.cjs` (in `make test-manifest`) | **Services views:** run through the real `fetchChainHealth`, using exact BCS and SDK-signed schema-2 reports. Forged, tampered, out-of-vocabulary, unknown-field, offline, delayed, revoked, chain-outage and missing-section reports are unavailable and carry no values. A verified version-2 report shows every section, the stream ranges and the export checkpoint; a version-1 report shows the positions as unavailable; an empty stream and an inconsistent range are covered; shadow documents point to the encrypted archive. **Alerts:** filtered per device. **Rollouts:** stage and threshold arithmetic, bake time, aborted and completed states, and twelve inconsistent-record refusals. Decision filtering, ordering and inconsistency, plus bounded event paging. **Archive search:** literal, case and source filters, bounds and truncation. **Access record:** bounded |
| `CHROME_EXECUTABLE=… node scripts/test_dashboard_browser.cjs` | The real Device services, Rollouts and Logs pages with the real dApp Kit, chain reader and verifier, a fixture wallet and a fullnode fixture (localnet manifest, exact BCS). **Services:** values and the alert drill-down are shown. A forged report hides all values and makes alerts unavailable. **Rollouts:** the aborted rollout's overview, stages and audit are shown. **Logs:** an unauthorized account gets no archive controls. **Archive search:** search, the access record and discard all work, with no web storage used. **Shadow documents:** a device-signed schema-2 archive is checked by the real verifier in the page. Its documents appear as text (markup is never interpreted), an imprecise integer is unavailable, and an older archive is labelled history. A tampered archive shows nothing |
| `make test-protocol-live` (disposable network-none localnet; `scripts/test_rollout_dashboard_live.cjs`) | **Rollouts on real chain data:** the dApp's own builders create a two-cohort rollout, pause it, skip an unadmitted member and admit the canary. The device reports a real failure and the next advance aborts at the threshold. The Rollouts readers (`readRollout`, `rolloutProgress`, `fetchRolloutDecisions` over gRPC) show the stages, grants, threshold, abort state and all five audited decisions. A resume after the abort fails in execution |
| `node scripts/test_shadow_documents.cjs` (in `make test-manifest`) | The shared cases with the daemon. Schema-2 binding to chain, fleet, device, sequence and signer. Refused: cross-schema, foreign, replayed, tampered and inconsistent-delta archives. Real Seal SDK encryption, opened through `fetchVerifiedArchive` with revocation, key epoch, storage-record and foreign-fleet refusals. The plaintext buffer is wiped |
| `scripts/test_fleet_data_ota.py` (in `make test-ota`) | The real daemon archives seeded shadows (schema 2), and archives again on a change at the routine cadence but not without one. Public health carries only the shadow count. The browser library decrypts and verifies every archive's documents |
| `make test-service-health` | Real daemon: an empty stream reports no range and no checkpoint. An overwriting stream reports the moved range with no checkpoint while the export fails. After recovery, the checkpoint reaches the newest sequence |
| `make edge-test`, `make indexer-test` | Daemon stream positions (empty, sentinel, discarded, crash gap). Archive schema 1/2 strictness, snapshot bounds and ordering, and the shared shadow cases. The indexer accepts services v1/v2 with the position rules, and the shared signed v2 vector |

Production Seal providers, the real extension wallet and IdP sessions, physical
devices and Mainnet remain unrun. They are tracked in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159). The browser
test uses a fixture wallet, not a manual wallet session.
