# SUI Edge

Decentralized edge device deployment system on the SUI blockchain — a trustless alternative to AWS Greengrass.

Operators register edge devices on-chain, upload firmware to Walrus (SUI's native blob storage), and create deployment transactions via a web dApp. Edge devices run a lightweight Rust daemon that subscribes to on-chain events, downloads firmware, applies it with SWUpdate, and reports status back to the chain.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Operator (browser)                                          │
│   dApp  ──── SUI wallet ────► Move contracts (testnet)      │
│   uploads firmware ────────► Walrus blob storage            │
└────────────────────────────────────┬────────────────────────┘
                                     │ DeploymentCreated event
                                     ▼
                          ┌──────────────────────┐
                          │ Edge device (Linux)  │
                          │   suifleet daemon │
                          │   ├─ subscribes WS   │
                          │   ├─ downloads blob  │
                          │   ├─ verifies sha256 │
                          │   ├─ runs swupdate   │
                          │   └─ reports status  │
                          └──────────────────────┘
```

**Stack:** Move · Next.js + @mysten/dapp-kit · Rust · SWUpdate · Walrus · SUI Testnet

---

## How it works

### On-chain objects

| Object | Type | Owned by |
|---|---|---|
| `DeviceRegistry` | Shared | — (admin is the deployer) |
| `Device` | Owned | Operator's wallet |
| `DeviceCap` | Owned | Device's own SUI address |
| `DeploymentRecord` | Shared | — |

### DeviceCap — the device's on-chain identity

When an operator registers a device, two things happen in one transaction:
1. A `Device` object is created and sent to the operator's wallet
2. A `DeviceCap` is created and sent to the device's SUI address

The edge daemon holds the private key for that address. Every `report_status` call requires presenting the `DeviceCap`, proving the caller controls the device's keypair. This means status reports can only come from the real device — not from the operator, and not from an impersonator.

### Device provisioning

The device needs an ed25519 keypair whose private key never leaves the device:

**Development / smoke test:**
```bash
openssl rand -hex 32 | tee /etc/suifleet/device.key
chmod 600 /etc/suifleet/device.key
```

**Production — software:**
A first-boot systemd oneshot service generates the key if it doesn't exist, derives the SUI address, and writes it to `/etc/suifleet/device.sui-address`. The operator reads this address (via SSH, local HTTP, or QR code display) and registers the device in the dApp.

**Production — TPM (recommended):**
The private key is generated inside the TPM chip and never exposed to the OS. The edge daemon routes signing through the TPM via PKCS#11 instead of reading a key file. Yocto's `meta-tpm` layer provides the full software stack. OEMs enable this with the `--features tpm` cargo flag. TPM attestation can additionally prove to the operator that the key lives in genuine hardware, not a software emulator.

The SUI address is derived from the public key:
```
address = sha3_256(0x00 || ed25519_pubkey_bytes)
```

---

## Prerequisites

- Docker (with your user in the `docker` group)
- GNU Make

---

## Quick start

```bash
# 1. Build the dev image and start the container
make dev

# 2. Open a shell inside the container
make shell

# 3. Run contract tests
make test-contracts

# 4. Install dApp deps and start the dev server (localhost:3000)
make dapp-install
make dapp-dev
```

> **First run:** `make dev` builds the Docker image from scratch — expect ~5–10 minutes.

---

## Development workflow

All `make` targets work from **both the host and inside the container**:

| Context | How it works |
|---|---|
| Host | `make <target>` shells into the container via `docker compose exec` |
| Inside container | `make <target>` runs the command directly (no Docker-in-Docker) |

### Reset volumes

If you see permission errors, named volumes may have stale root-owned data. Tear down and start fresh:

```bash
make down   # removes containers AND volumes
make dev
```

---

## Contracts

Move smart contracts in `contracts/`. Three modules:

- `device_registry` — register/deregister edge devices; each `Device` records the device's own SUI address
- `device_cap` — `DeviceCap` capability object issued to the device's address at registration; authorises status reports
- `deployment_manager` — create deployments targeting devices, track per-device status on-chain

```bash
make build-contracts      # sui move build
make test-contracts       # sui move test (6 tests)
make publish-contracts    # publish or upgrade via scripts/redeploy.sh
```

`scripts/redeploy.sh` auto-detects whether the package has been published before and uses `sui client upgrade` or `sui client publish` accordingly. It writes the new IDs to `dapp/.env.local`.

### SUI wallet setup (first time)

```bash
make shell
sui client new-env --alias testnet --rpc https://fullnode.testnet.sui.io:443
sui client switch --env testnet
sui client new-address ed25519
sui client faucet
```

---

## dApp

Next.js app in `dapp/`. Pages:

| Route | Purpose |
|---|---|
| `/` | Dashboard — device count, recent deployments |
| `/devices` | Register devices (auto-issues DeviceCap), list owned Device objects |
| `/firmware` | Upload `.swu` to Walrus, create deployment transaction |
| `/deployments` | Track per-device deployment status |

```bash
make dapp-install    # pnpm install (first time / after adding deps)
make dapp-dev        # start dev server at http://localhost:3000
make dapp-build      # production build
```

The dApp is also deployable as a static site on Walrus Sites — see [Walrus hosting](#walrus-hosting) below.

---

## Edge client

Rust daemon in `suifleet-edge/`. Runs on the edge device (Pi 3 B+ / aarch64), subscribes to SUI WebSocket events, downloads firmware blobs from Walrus, verifies sha256, invokes `swupdate`, and reports status on-chain using the device's `DeviceCap`.

```bash
make edge-build      # cargo build --release
make edge-test       # cargo test
```

### Device provisioning

1. Generate a keypair on the device:
   ```bash
   openssl rand -hex 32 | tee /etc/suifleet/device.key
   chmod 600 /etc/suifleet/device.key
   ```
2. Derive the SUI address:
   ```bash
   suifleet --derive-address /etc/suifleet/device.key
   ```
3. In the dApp **Devices** page, register the device using that address. One transaction creates the `Device` object in your wallet and sends the `DeviceCap` to the device's address.
4. Note the `Device` object ID from the dApp, and find the `DeviceCap` object ID:
   ```bash
   sui client objects --address <device-sui-address>
   ```
5. Fill in `config.toml`:

```toml
[sui]
rpc_url    = "https://fullnode.testnet.sui.io:443"
ws_url     = "wss://fullnode.testnet.sui.io:443"
package_id = "0x<PACKAGE_ID>"

[device]
keypair_path     = "/etc/suifleet/device.key"
device_object_id = "0x<DEVICE_OBJECT_ID>"
device_cap_id    = "0x<DEVICE_CAP_ID>"

[walrus]
aggregator_url = "https://aggregator.walrus-testnet.walrus.space"

[swupdate]
binary  = "/usr/bin/swupdate"
dry_run = false        # set true to skip the actual flash during testing
```

```bash
EDGE_CONFIG=/etc/suifleet/config.toml suifleet
```

---

## Walrus hosting

The dApp can be deployed as a static site on [Walrus Sites](https://docs.walrus.site) — no server required, served directly from Walrus blob storage via a SUI object.

```bash
# Build a static export
make dapp-build

# Publish to Walrus Sites (requires walrus site-builder CLI)
walrus site-builder publish dapp/out --epochs 10
```

The `site-builder` prints a SUI object ID. Access the site at:
```
https://<object-id>.wal.app
```

---

## OpenEmbedded / Yocto

The `suifleet-edge/meta-suifleet/` directory is a Yocto layer ready to drop into a Yocto build targeting `raspberrypi4-64` (or any aarch64 machine):

```bash
bitbake-layers add-layer /path/to/suifleet/suifleet-edge/meta-suifleet
echo 'IMAGE_INSTALL:append = " suifleet"' >> conf/local.conf
bitbake core-image-minimal
```

The recipe cross-compiles the Rust daemon via the `cargo` Yocto class, installs the systemd unit, and declares `swupdate` as a runtime dependency.

TPM support can be enabled by adding `PACKAGECONFIG:append = " tpm"` to your `local.conf` and including `meta-tpm` in your layer stack.
