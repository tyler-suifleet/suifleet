# SF-142 software validation

Codex, running model `gpt-6.1-sol`, resumed session
`dispatch-4766dc36941742879b892b18a1214a67` after SF-131 merged.
`origin/main` was fetched and fast-forward merged at
`3b7f364dbb2d8eaec81151b4eb639f99b745df31` before implementation.
Tests below use locked frontend dependencies installed with
`pnpm install --frozen-lockfile` in a dedicated `sf142-node` Docker volume.
No existing application's wallet, fleet objects or device state were used.

Passed software checks:

- `make test-data-metering`: proposed allowance, decimal GB/GB-month,
  excess retention, configurable rates and invalid inputs; surcharge after
  minimum/annual discount; combined log/extra data deposit; paginated typed
  records, per-device and owner counters, expired records, clipped byte-time,
  released lifetime writes and binding/type/cursor/concurrent-mutation failures.
- `make test-fleet-settings`: existing budget, settings and estimator regressions
  and the real Chromium settings fixture. Added browser assertions show that
  additional device data increases gas/deposit and proposed price with logs off,
  displays allowance/rates and returns to baseline when volume returns to zero.
  Existing wallet refusal, saved-fleet switch, save warning and effects checks
  remain covered by the fixture.
- `make dapp-typecheck`: frontend typecheck, 80 offline testnet-controller tests,
  and manifest regressions. These controller tests do not perform public writes.
- `make dapp-build`: successful Next.js static production export.
- `python3 scripts/test_protocol_localnet.py --retention-upgrade`: disposable
  loopback-only chain with real pinned Walrus sources, existing-object compatible
  upgrade, old-package rejection and the real frontend meter. Two registered
  records (10 and 11 logical bytes) reconcile to 21 logical bytes and 1,326,800
  lifetime encoded bytes, preserving original type origins after upgrade.
  [Sanitized IDs, confirmed digests and gas evidence](sf142-localnet.json).
  The deterministic upstream Walrus fixture is local test setup; this does not
  establish public Walrus certification or provider availability.

Reproduction from this worktree, after warming the pinned Move cache in a private
`sf142-move` volume:

```bash
# Shared Git metadata is read-only, required by the historical upgrade fixture.
docker run --rm --network none -e INSIDE_DEV_CONTAINER=1 \
  -v "$PWD:/workspace" -v sf142-node:/workspace/dapp/node_modules \
  -w /workspace suifleet-dev:sf035 \
  bash -lc 'make test-data-metering dapp-typecheck dapp-build'
docker run --rm --network none -e INSIDE_DEV_CONTAINER=1 \
  -e CHROME_EXECUTABLE=/opt/chrome-headless-shell/chrome-headless-shell \
  -v "$PWD:/workspace" -v sf142-node:/workspace/dapp/node_modules \
  -w /workspace suifleet-dev:sf010-chrome make test-fleet-settings
docker run --rm --network none -v "$PWD:/workspace" \
  -v /home/tyler/dev/codex/src/suifleet/.git:/home/tyler/dev/codex/src/suifleet/.git:ro \
  -v sf142-node:/workspace/dapp/node_modules -v sf142-move:/home/dev/.move \
  -w /workspace suifleet-dev:sf035 \
  python3 scripts/test_protocol_localnet.py --retention-upgrade
```

Resolved environment attempts: the canonical checkout's installed dependencies
were outdated, so its temporary symlink was removed and a fresh locked private
volume installed. The first install needed the volume assigned to container UID
1001. The first localnet attempt lacked read-only shared Git metadata. Running
`make dapp-typecheck` in the older Chrome image failed its toolchain test because
that image lacks `/opt/sui-network/sui`; the complete target passes in `sf035`,
which contains the pinned CLI. No failed assertion was ignored or gate removed.

Limitations: per-stream means uploader device, not per-kind application billing.
Stored byte-time covers retained records only and projects the current epoch
schedule; released historical records are not reconstructed. Lifetime encoded
counters are never sold as logical monthly writes. Prices are proposals and
steady-state planning estimates; no charging engine or billing ledger is claimed.
No Move layout/API or migration is changed by SF-142. Hosted exact-head/current-base
CI, automatic testnet and independent review remain coordinator gates after push;
they are not claimed passed here. Final physical/Mainnet/provider/manual-wallet
acceptance remains unrun in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
