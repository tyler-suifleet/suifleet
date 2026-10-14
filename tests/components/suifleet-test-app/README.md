# SuiFleet test component (SF-030)

A harmless, versioned OTA payload that proves which artifact actually ran on a device. It uses the current one-shot apply path: no supervisor, no recipe commands and no configuration. It writes only below its own directory. It never touches boot partitions, firmware or services.

| File | Role |
|---|---|
| `app.sh.in` | Template for the application artifact (POSIX `sh`). Its `install`, `run` and `health` subcommands are invoked by the updater. |
| `suifleet-test-updater` | One-shot updater, configured as the daemon's `apply.command` on test devices. It runs with busybox. |
| `../../../scripts/test_component.py` | Deterministic artifact and recipe generator. |
| `../../../suifleet-edge/meta-suifleet/recipes-edge/suifleet-test-component` | Yocto recipe that installs the updater as `/usr/bin/suifleet-test-updater` in test images. |

## Build artifacts and recipes

```bash
python3 scripts/test_component.py build --version 1.0.0 --run-id RUN --out app-v1.sh
python3 scripts/test_component.py build --version 2.0.0 --run-id RUN --out app-v2.sh
python3 scripts/test_component.py build --version 3.0.0 --run-id RUN --fail install --out app-install-fail.sh
python3 scripts/test_component.py build --version 4.0.0 --run-id RUN --fail health --out app-health-fail.sh
# After uploading an artifact to Walrus:
python3 scripts/test_component.py recipe --artifact app-v1.sh --blob-id ARTIFACT_BLOB_ID --out recipe-v1.json
```

Each command prints the output's SHA-256. The same inputs always produce the same bytes.

- The **run ID** is baked into the artifact and echoed in the device receipt. Use a fresh one per acceptance run, so a receipt can only come from this run's artifacts.
- **Injected failures** are part of the artifact, so they are authenticated like any other update.
- **Versions, component names and run IDs** must match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`.

Deploy with component name `suifleet-test-app` and the artifact's version. The recipe's `artifactSha256` must be the on-chain artifact hash, and the recipe's own SHA-256 is the deployment's recipe digest.

## Device configuration

Test devices only. Production devices keep their real firmware updater.

```toml
[apply]
command = "/usr/bin/suifleet-test-updater"   # optionally: --root DIR
```

The updater writes under `/var/lib/suifleet/test-components/suifleet-test-app/` by default. This is inside the daemon's own state directory, so it stays writable under the sandboxed service.

| Path | Contents |
|---|---|
| `versions/<version>/app` | Installed artifact |
| `current` | Installed version |
| `receipt.json` | Latest attempt |
| `history.jsonl` | Every attempt, oldest first |

Example receipt:

```json
{"schema":1,"component":"suifleet-test-app","version":"2.0.0","runId":"RUN","artifactSha256":"…",
 "previousVersion":"1.0.0","install":"succeeded","health":"passed",
 "run":{"component":"suifleet-test-app","version":"2.0.0","runId":"RUN","arch":"aarch64",
        "bootId":"…","executedAt":"2026-10-14T17:00:00Z"},"receiptAt":"2026-10-14T17:00:00Z"}
```

`run` is printed by the installed application itself. The updater accepts it only if it names the installed version and run ID.

| Outcome | Updater exit | Deployment status | Receipt |
|---|---|---|---|
| Installed and healthy | 0 | Applied | `install: succeeded`, `health: passed` |
| Rejected artifact (bad header, symlink, over 64 KiB) | 2 | Failed | none written |
| Install failed | 10 | Failed | `install: failed`, `health: not-run`; previous version stays current |
| Installed but run or health failed | 20 | Failed | `install: succeeded`, `health: failed` (or `not-run` if the app did not identify itself) |

The chain records only Applied or Failed. The receipt is what distinguishes an install failure from a runtime-health failure.

Rollback, a supervised run/stop lifecycle and configuration are not implemented. When those features land, extend this component with matching modes rather than inferring them from a one-shot update.

## Tests

`make test-component` runs:

- **`scripts/tests/test_test_component.py`**, offline. It covers determinism, unsafe identifiers, v1→v2 receipts, install versus health failure, malformed and symlinked artifacts, and that writes stay under the root. It runs under the system shell and, when busybox is installed, again with a busybox-only `PATH` like the Yocto image.
- **`scripts/test_component_ota.py`**: the real daemon against the local chain/Walrus simulator, using the real updater for v1, v2, an install failure and a health failure. It checks on-chain statuses, reports and receipts.

The Raspberry Pi run is SF-031. Take `ticket lock hardware:raspberrypi3bplus` only while flashing or testing.
