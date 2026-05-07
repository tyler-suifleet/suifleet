# SUI Greengrass — Decentralized Edge Deployment System

> Decentralized competitor to AWS Greengrass on the SUI blockchain.
> Operators connect via SUI wallet and deploy firmware builds to registered edge devices.
> Devices are identified by SUI keypairs, firmware stored on Walrus, deployments are on-chain transactions, and status reports are signed by the device itself.

**Stack:** Move (contracts) · Next.js + @mysten/dapp-kit (dApp) · Rust (edge daemon) · SWUpdate (OTA) · Walrus (storage) · SUI Testnet

---

## Repository Layout

```
sui/
├── PLAN.md
├── contracts/                        # Move smart contracts
│   ├── Move.toml
│   └── sources/
│       ├── device_registry.move
│       ├── deployment_manager.move
│       └── device_cap.move
├── dapp/                             # Next.js dApp
│   ├── package.json
│   ├── next.config.ts
│   ├── tailwind.config.ts
│   └── src/
│       ├── app/
│       │   ├── layout.tsx
│       │   ├── page.tsx              # Dashboard
│       │   ├── devices/page.tsx
│       │   ├── firmware/page.tsx
│       │   └── deployments/page.tsx
│       ├── components/
│       │   ├── WalletConnect.tsx
│       │   ├── DeviceCard.tsx
│       │   ├── DeploymentRow.tsx
│       │   └── FirmwareUpload.tsx
│       ├── hooks/
│       │   ├── useDevices.ts
│       │   └── useDeployments.ts
│       └── lib/
│           ├── sui.ts                # contract call helpers
│           └── walrus.ts             # Walrus upload/download
└── edge-client/                      # Rust daemon + OE recipe
    ├── Cargo.toml
    ├── src/
    │   ├── main.rs
    │   ├── config.rs
    │   ├── sui_client.rs
    │   ├── walrus_client.rs
    │   └── swupdate.rs
    └── openembedded/
        ├── conf/
        │   └── layer.conf
        └── recipes-edge/
            ├── edge-client_1.0.bb
            └── files/
                └── edge-client.service
```

---

## Phase 1 — Move Smart Contracts

### 1.1 Scaffold
```
sui move new greengrass-sui   # inside sui/contracts/
```
`Move.toml` targets SUI testnet; add SUI framework dependency.

### 1.2 `device_registry.move`

**Shared object** `DeviceRegistry` (one global registry, owned by admin):
```move
struct DeviceRegistry has key { id: UID, admin: address }
```

**Owned object** `Device` (transferred to the registering wallet):
```move
struct Device has key, store {
    id: UID,
    name: String,             // human label
    device_address: address,  // device's own SUI keypair address
    arch: String,             // e.g. "aarch64"
    meta: VecMap<String, String>,
    registered_at: u64,
}
```

Functions: `register_device`, `deregister_device`, `update_metadata`

### 1.3 `device_cap.move`

`DeviceCap` — capability object issued to the device's SUI address on registration.
Authorizes the device to submit on-chain status reports.

```move
struct DeviceCap has key, store {
    id: UID,
    device_id: ID,   // points to Device object
}
```

`issue_device_cap(device: &Device, ctx): DeviceCap` — called during registration, transferred to `device.device_address`.

### 1.4 `deployment_manager.move`

**Shared object** `DeploymentRecord`:
```move
struct DeploymentRecord has key {
    id: UID,
    creator: address,
    firmware_name: String,
    version: String,
    walrus_blob_id: String,      // Walrus blob ID (32-byte hex)
    sha256_hash: String,         // hex sha256 of the .swu file
    target_devices: vector<ID>,  // Device object IDs
    statuses: VecMap<ID, u8>,    // 0=pending  1=applied  2=failed
    created_at: u64,
}
```

**Events** (consumed by edge clients via WebSocket subscription):
```move
struct DeploymentCreated has copy, drop {
    deployment_id: ID,
    walrus_blob_id: String,
    sha256_hash: String,
    target_devices: vector<ID>,
}

struct DeviceStatusUpdated has copy, drop {
    deployment_id: ID,
    device_id: ID,
    status: u8,
}
```

Functions:
- `create_deployment(registry, firmware_name, version, walrus_blob_id, sha256_hash, target_device_ids, clock, ctx): DeploymentRecord`
- `report_status(deployment: &mut DeploymentRecord, cap: &DeviceCap, status: u8, clock, ctx)` — called by the device

### 1.5 Tests
Move unit tests in `contracts/tests/`:
- Register / deregister device
- Create deployment targeting device
- Device reports applied / failed status
- Unauthorized status report rejected

### 1.6 Deploy to Testnet
```
sui client publish --gas-budget 100000000
```
Save `PACKAGE_ID`, `REGISTRY_ID` to `sui/contracts/.env.testnet`.

---

## Phase 2 — dApp

### 2.1 Scaffold
```
pnpm create next-app dapp --typescript --tailwind --app
cd dapp && pnpm add @mysten/dapp-kit @mysten/sui @tanstack/react-query
```

### 2.2 Wallet Provider (`src/app/layout.tsx`)
Wrap app in `SuiClientProvider` (testnet) + `WalletProvider` + `QueryClientProvider`.

### 2.3 Contract Call Helpers (`src/lib/sui.ts`)
- `registerDevice(txb, registryId, name, deviceAddress, arch)` — builds PTB
- `createDeployment(txb, registryId, firmwareName, version, blobId, hash, deviceIds)` — builds PTB
- `queryDevices(suiClient, owner)` — `getOwnedObjects` filtered by `Device` type
- `queryDeployments(suiClient)` — `queryEvents` by `DeploymentCreated`

### 2.4 Walrus Helper (`src/lib/walrus.ts`)
- `uploadFirmware(file: File): Promise<{ blobId: string, sha256: string }>`
  - Compute sha256 client-side via Web Crypto API before upload
  - POST to Walrus publisher endpoint (`https://publisher.walrus-testnet.walrus.space/v1/store`)
- `getBlobUrl(blobId: string): string` — aggregator URL for download

### 2.5 Pages

| Page | Description |
|---|---|
| `/devices` | List `Device` objects owned by wallet; register new device modal |
| `/firmware` | Drag-drop upload → Walrus → display blob ID + sha256; trigger deployment |
| `/deployments` | Table of `DeploymentRecord` objects; per-device status badges; live refresh |
| `/` | Dashboard: summary counts + recent deployment activity |

### 2.6 Hooks
- `useDevices()` — `useQuery` wrapping `queryDevices`
- `useDeployments()` — `useQuery` wrapping `queryEvents`, 10s refetch interval

---

## Phase 3 — Edge Client (Rust)

### 3.1 Dependencies (`Cargo.toml`)
```toml
sui-sdk = { git = "https://github.com/MystenLabs/sui", package = "sui-sdk" }
tokio = { features = ["full"] }
reqwest = { features = ["stream"] }
sha2 = "0.10"
serde = { features = ["derive"] }
serde_json = "1"
toml = "0.8"
tracing = "0.1"
tracing-subscriber = "0.3"
```

### 3.2 Config (`src/config.rs`)
Loaded from `/etc/edge-client/config.toml`:
```toml
[sui]
rpc_url = "https://fullnode.testnet.sui.io:443"
ws_url  = "wss://fullnode.testnet.sui.io:443"
package_id = "0x..."
registry_id = "0x..."

[device]
keypair_path = "/etc/edge-client/device.key"
device_object_id = "0x..."
device_cap_id = "0x..."

[walrus]
aggregator_url = "https://aggregator.walrus-testnet.walrus.space"

[swupdate]
binary = "/usr/bin/swupdate"
```

### 3.3 SUI Client (`src/sui_client.rs`)
- Load ed25519 keypair from file
- `subscribe_deployments()` — WebSocket subscription to `DeploymentCreated` events filtered by `package_id`
- `report_status(deployment_id, cap_id, status: u8)` — build + sign + submit PTB calling `deployment_manager::report_status`

### 3.4 Walrus Client (`src/walrus_client.rs`)
- `download_blob(blob_id, dest_path)` — GET `{aggregator}/v1/{blob_id}`, stream to file

### 3.5 SWUpdate (`src/swupdate.rs`)
- `apply_update(swu_path, expected_hash)` — verify sha256 first, then `Command::new(swupdate_binary).arg("-i").arg(swu_path).status()`
- Returns `Ok(())` or `Err`

### 3.6 Main Loop (`src/main.rs`)
```
loop {
  connect WebSocket, subscribe DeploymentCreated events
  on event:
    if device_object_id in target_devices:
      download blob → temp file
      verify sha256
      invoke swupdate
      report_status(Applied | Failed)
  on disconnect: exponential backoff reconnect
}
```

systemd watchdog: `sd_notify(WATCHDOG=1)` every 30s via `sd-notify` crate.

---

## Phase 4 — OpenEmbedded Layer

### 4.1 Layer Structure
```
openembedded/
├── conf/
│   └── layer.conf          # BBPATH, layer name, deps on meta, meta-oe
└── recipes-edge/
    ├── edge-client_1.0.bb
    └── files/
        └── edge-client.service
```

### 4.2 Recipe (`edge-client_1.0.bb`)
```bitbake
SUMMARY = "SUI edge deployment client"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=..."

SRC_URI = "git://github.com/<org>/sui-greengrass;protocol=https;branch=main \
           file://edge-client.service"
SRCREV = "${AUTOREV}"
S = "${WORKDIR}/git/edge-client"

inherit cargo systemd pkgconfig

DEPENDS += "openssl"
RDEPENDS:${PN} += "swupdate"

SYSTEMD_SERVICE:${PN} = "edge-client.service"
SYSTEMD_AUTO_ENABLE:${PN} = "enable"

do_install:append() {
    install -d ${D}${sysconfdir}/edge-client
    # config.toml and device.key provisioned out-of-band
}
```

### 4.3 Systemd Unit (`edge-client.service`)
```ini
[Unit]
Description=SUI Edge Deployment Client
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/edge-client
Restart=on-failure
RestartSec=5
WatchdogSec=60
NotifyAccess=main
EnvironmentFile=-/etc/edge-client/env

[Install]
WantedBy=multi-user.target
```

### 4.4 Device Provisioning (`edge-client-conf_1.0.bb`)
Generates a fresh ed25519 keypair on first boot, writes `device.key` to `/etc/edge-client/`. Operator then registers this address via the dApp.

---

## Phase 5 — Integration Test Plan

1. Deploy contracts to testnet; record `PACKAGE_ID` + `REGISTRY_ID`.
2. Run dApp (`pnpm dev`); connect SUI wallet; register a test device with a generated keypair address.
3. Build edge client locally (`cargo build`); configure it with testnet + device object IDs.
4. Upload a dummy `.swu` via the dApp firmware page; confirm blob ID from Walrus.
5. Create a deployment targeting the test device; verify `DeploymentCreated` event on-chain.
6. Edge client receives event, downloads blob, verifies hash, invokes `swupdate --dry-run`, submits `report_status(Applied)`.
7. dApp deployments page shows device status: `pending` → `applied`.
8. Repeat with a bad hash to verify the `Failed` path.

---

## TODOs

### Contracts
- [ ] `sui/contracts/Move.toml`
- [ ] `sui/contracts/sources/device_registry.move`
- [ ] `sui/contracts/sources/device_cap.move`
- [ ] `sui/contracts/sources/deployment_manager.move`
- [ ] `sui/contracts/tests/device_registry_tests.move`
- [ ] `sui/contracts/tests/deployment_manager_tests.move`
- [ ] Deploy to testnet; save `.env.testnet`

### dApp
- [ ] Scaffold Next.js app at `sui/dapp/`
- [ ] `sui/dapp/src/app/layout.tsx` — wallet + query providers
- [ ] `sui/dapp/src/lib/sui.ts`
- [ ] `sui/dapp/src/lib/walrus.ts`
- [ ] `sui/dapp/src/hooks/useDevices.ts`
- [ ] `sui/dapp/src/hooks/useDeployments.ts`
- [ ] `sui/dapp/src/app/devices/page.tsx`
- [ ] `sui/dapp/src/app/firmware/page.tsx`
- [ ] `sui/dapp/src/app/deployments/page.tsx`
- [ ] `sui/dapp/src/app/page.tsx`

### Edge Client
- [ ] `sui/edge-client/Cargo.toml`
- [ ] `sui/edge-client/src/config.rs`
- [ ] `sui/edge-client/src/sui_client.rs`
- [ ] `sui/edge-client/src/walrus_client.rs`
- [ ] `sui/edge-client/src/swupdate.rs`
- [ ] `sui/edge-client/src/main.rs`

### OpenEmbedded
- [ ] `sui/edge-client/openembedded/conf/layer.conf`
- [ ] `sui/edge-client/openembedded/recipes-edge/edge-client_1.0.bb`
- [ ] `sui/edge-client/openembedded/recipes-edge/files/edge-client.service`
- [ ] `sui/edge-client/openembedded/recipes-edge/edge-client-conf_1.0.bb`

### Integration
- [ ] End-to-end test script / README for running locally with Docker (simulated device)
