# Confidential delivery with Seal (SF-022)

Proprietary artifacts and recipes are encrypted in the uploader's browser with [Seal](https://docs.sui.io/sui-stack/seal) before anything reaches a Walrus publisher. Devices obtain decryption keys with their own enrolled identity, decrypt locally and verify the on-chain digests before executing anything. Explicitly public delivery stays available for public or open-source content. Neither path falls back to the other.

This is a commercial-prototype baseline, not a KMS/HSM service or a home for regulated data. See [Limits](#limits).

## Pieces

| Piece | Where | Role |
|---|---|---|
| Access policy | [`seal_policy.move`](../contracts/sources/seal_policy.move) | Who may obtain keys: accounts, devices, device groups |
| Encryption | [`dapp/src/lib/seal.ts`](../dapp/src/lib/seal.ts), [`walrus.ts`](../dapp/src/lib/walrus.ts) | Seal-encrypts before upload; confidential by default |
| Policy UI | `/access` ([`access-policy.ts`](../dapp/src/lib/access-policy.ts)) | Create, grant, revoke, rotate |
| Edge client | [`seal.rs`](../suifleet-edge/src/seal.rs), [`seal_client.rs`](../suifleet-edge/src/seal_client.rs) | Sessions, key fetch, decryption, verification |
| Session holder | [signing service](KEY_ISOLATION.md) `seal` intent | Certifies sessions with the device key; holds the session key |

## Access policies

An `AccessPolicy` is a shared object. Its holder of `AccessAdminCap` grants and revokes three scopes with ordinary signed transactions:

- **Account**: a Sui address, which decrypts with its own key (for example a human reviewer using the official SDK).
- **Device**: an activated device by registry ID. Keys are released only to a session certified by that device's current, unrevoked registry key. Revoking, rotating or deregistering the device in the registry therefore also stops key release.
- **Group**: a device group. Only the group's own admin can grant it, because the group admin decides membership. Devices currently in the group, with active keys, qualify.

**Authority is separate.** Decryption access grants nothing else. Publishing, deploying and administering devices stay with their own Move capabilities and roles ([ACCESS_AND_ENCRYPTION.md](ACCESS_AND_ENCRYPTION.md)). Possessing a key or plaintext authorizes no operation, and the frontend only displays what the chain enforces.

**Transfer and delegation.** `AccessAdminCap` has `key, store` and is transferable: whoever holds it administers that policy, and transferring it moves that authority entirely. It cannot be split or scoped. A group grant delegates membership decisions to that group's admin.

## Identities and versions

A Seal key ID is `namespace || identity`:

- **Namespace** is the fleet package's **original** (first-version) ID, Seal's rule for upgradeable packages. The `seal_policy` module arrives by upgrade, so approvals call the latest package while keys stay in the original namespace.
- **Identity** is `policy ID (32 bytes) || key epoch (u64 little-endian) || random nonce (16-64 bytes)`. The dapp uses 32 random bytes per release, shared by its artifact and recipe so a device fetches one key.

`seal_approve_*` functions are non-public `entry` functions without side effects. **Old package versions remain callable** by key servers, so every approval and admin call requires `AccessPolicy.version == VERSION` of the executing code. A future policy change bumps `VERSION` and `migrate`s each policy; older code then refuses it. The Move tests demonstrate this bypass and its refusal.

`rotate_key_epoch` retires **every** earlier identity of the policy at once. New releases must use the new epoch, and anything still needed must be re-encrypted and redeployed.

## Upload and binding

1. The browser computes the plaintext SHA-256.
2. It encrypts with the pinned `@mysten/seal` SDK: AES-256-GCM, threshold *t* of the configured key servers. Key servers are read from chain and must prove possession of their keys first.
3. It re-parses the result and checks package, identity, AAD, length and threshold, then uploads `MAGIC || EncryptedObject` (`MAGIC` is `suifleet/seal/v1\n`).
4. Any failure aborts with nothing uploaded. A public upload needs an explicit acknowledgement.

The deployment records the **plaintext** digests, exactly as for public delivery.

**Associated data.** The AAD is `suifleet/seal/v1/<artifact|recipe>/<plaintext sha256>`. The device requires the AAD to name the expected kind and on-chain digest. It then decrypts, authenticating the ciphertext, and still compares the plaintext digest with the chain. Anyone can encrypt to a Seal identity, so a ciphertext alone proves nothing; the on-chain digest is the integrity anchor.

**Public:**
- the magic and the Seal object metadata: namespace, policy ID, key epoch, nonce, key server IDs, threshold;
- the AAD, and therefore the plaintext SHA-256, which is also on chain;
- blob sizes, which Seal does not hide;
- deployment records.

**Guessable content.** A plaintext digest confirms guesses of low-entropy content. Do not put guessable secrets in a recipe. Confidential configuration values still come from the device overlay ([OPERATIONS.md](OPERATIONS.md)).

## Edge authentication and decryption

- **Sessions.** The device signs Seal's session certificate (`Accessing keys of package <namespace> for <ttl> mins from <UTC>, session key <base64>`) as a Sui personal message with its own key: the ed25519 key file, or the P-256 PKCS#11 signer (its personal-message path is unit-tested only). Each request is signed by the ephemeral session key.
  - **Lifetime.** 1-30 minutes, default 10. The certificate is dated back by `clock_skew_seconds` (default 60), because key servers refuse a creation time ahead of their clock. Sessions are renewed a minute before expiry and never written to disk.
  - **Signing service.** With `kind = "service"`, the [signing service](KEY_ISOLATION.md) holds the session key. It signs only key requests that are `seal_policy::seal_approve*` calls on the fleet package, with the identity first and read-only shared objects, plus a consistent ElGamal key pair. Only the daemon's main process may ask.
  - No administrator key, seed or signature is involved, and the administrator never approves individual downloads.
- **Key servers** are configured by object ID only. Their URL and public key are read from their on-chain `key_server::KeyServer` objects of the configured Seal package. Independent v1/v2 servers are supported; committee/aggregator servers are refused. Transport must be HTTPS, or plain HTTP to loopback only. Each server must pass its `/v1/service` proof of possession before it is asked for keys.
- **Key requests.**
  - The daemon reads the policy and its groups, checks the identity's key epoch, and builds one approval call: `seal_approve_device`, or `seal_approve_group_device` through the first granted group containing it.
  - It asks every needed key server concurrently with a fresh ElGamal key.
  - It checks each returned key against that server's public key, then needs servers worth the object's threshold. That threshold must be at least `min_threshold`.
  - Every key server must be configured and verified.
- **Decryption** happens in memory, bounded by `max_plaintext_bytes` (default 128 MiB, at most `walrus.max_artifact_bytes`). The shares and their consistency are verified against every key server's public key. The DEM is authenticated, and the plaintext digest is checked before the plaintext is written, with mode 0600, to the deployment's working path. The DEM key and the decrypted blob buffer are zeroized when freed; parsed recipe data and fetched user keys are ordinary memory.
- **Required mode.** `mode = "required"` refuses unencrypted recipes and artifacts, so a deployment made with public delivery fails. `optional` (the default) decrypts confidential blobs and applies explicitly public ones.
- **Failures.** A refused, unavailable or inconsistent key fetch fails the deployment, which is reported Failed on chain. Nothing is retried automatically; fix the cause and redeploy.

## Lifetimes, caching, offline use and revocation

- **Session keys and certificates**: memory only, at most `session_ttl_minutes`.
- **Fetched decryption keys** (per identity and key server): memory only, at most `key_cache_seconds` (default 120). Set it to 0 to fetch for every blob. They are lost on restart.
- **Confidential plaintext**: present only in the deployment's working path and what the component itself installs. It is never put in the offline component cache, never logged, and never written to the journal. In `required` mode the plaintext-only offline cache is not consulted.
- **Offline execution.**
  - Installed components keep running offline.
  - A new or reinstalled confidential version needs reachable key servers.
  - `offline_candidates = "cached"` serves only public content.
- **Revocation** stops future key release once key servers observe it. Key servers evaluate chain state independently and asynchronously, and some may lag. Revocation is not atomic across servers or with other chain changes.
- **What revocation cannot do.** It cannot recall keys or plaintext already obtained, or uninstall software already delivered. Within `key_cache_seconds` a running daemon may reuse an already-fetched key for the same identity.
- **Protecting future versions.** Use new releases. Each release already has a fresh identity. Use `rotate_key_epoch` to retire every earlier release, and re-encrypt whatever devices still need.
- **Disk cleanup.** Ciphertext downloads are deleted after decryption. Leftover working files are removed at the next start, as for public artifacts.

## Key providers, credentials, costs and availability

**Testnet.** Mysten's open-mode testnet key servers need no credential. They were used for the live check:
- `mysten-testnet-1`, `0x73d05d62c18d9374e3ea529e8e0ed6161da1a141a94d3f76ae3fe4e99356db75`;
- `mysten-testnet-2`, `0xf5d14a81a982144ae441cd7d64b09027f116a468bd36e7eca494f750591623c8`;
- both are objects of Seal package `0x0f16e84a49dec8425e6900cfdfe3730aaf1e8bc608d9f0500fcfa2c2267abfb4`.

They carry no SLA and no promise of long-term key persistence, so do not encrypt data you must decrypt reliably later with only testnet servers ([Seal providers](https://docs.sui.io/sui-stack/seal/pricing)).

**Mainnet** is not configured or authorized here. Choosing providers is a release decision:
- Independent providers (Ruby Nodes, NodeInfra, Overclock, Studio Mirai, H2O Nodes, Triton One, Natsai, Enoki) set their own prices and rate limits. Prices were not obtained or measured.
- Mysten's mainnet committee server needs an Enoki API key and an aggregator, which this edge client does not support yet.
- Choose a threshold and a set of independent providers you can rely on for as long as the data must stay decryptable. The server set is fixed per ciphertext, so a provider's loss of keys loses the data unless enough others remain.
- Seal has no on-chain audit log of key delivery.

**Provider API credentials** identify a paying client, not a device, and grant no decryption.
- **Edge.** Optional per key server: `api_key_name` (for example `X-API-Key`) and `api_key_file`, read at runtime from a file that other users cannot read.
- **Under the daemon user.** Components share that user, so they can read it. Use per-device, rate-limited credentials.
- **Frontend.** It accepts only object IDs and weights, and refuses a credential in its settings. Credentials must never be baked into frontend bundles or images.

**Measured cost (testnet, 2026-10-16).**
- **Encrypted delivery** on an existing fixture: 7,239,200 MIST net from the wallet. That covers policy creation (one-time storage), the device grant, and a deployment created and deleted. The device's report used 1,251,568 MIST of its own gas. Key servers charged nothing.
- **Fresh package-upgrade fixture** for this head: 444,879,325 MIST. This is package storage, unrelated to Seal itself.

The full network/service bill model remains [SF-054](https://github.com/tyler-suifleet/suifleet/issues/97).

## Configuration

**Frontend** (`.env.local`, public values only):

```bash
NEXT_PUBLIC_SEAL_KEY_SERVERS='[{"objectId":"0x73d0…db75","weight":1},{"objectId":"0xf5d1…23c8","weight":1}]'
NEXT_PUBLIC_SEAL_THRESHOLD=2
```

**Daemon** (`config.toml`; device templates may contain `[seal]`, as inline arrays only):

```toml
[seal]
mode = "required"                       # or "optional" (default)
key_server_package = "0x0f16…bfb4"      # Seal package of the key server objects
key_servers = [{ object_id = "0x73d0…db75" }, { object_id = "0xf5d1…23c8" }]
min_threshold = 2
# verify_key_servers = true  session_ttl_minutes = 10  clock_skew_seconds = 60
# key_cache_seconds = 120  request_timeout_seconds = 10  max_plaintext_bytes = 134217728
# Per server, optional: api_key_name = "X-API-Key", api_key_file = "/etc/suifleet/seal-api-key"
```

**Signing service** (`signer.toml`), with `[device.signer] kind = "service"`:

```toml
[seal]
session_ttl_minutes = 10
clock_skew_seconds = 60
```

Device clocks must be roughly correct: a clock behind by more than the session lifetime, or ahead by more than `clock_skew_seconds`, gets its certificates refused.

## Evidence

- **Move:** `make test-contracts`, including 17 `seal_policy` tests:
  - scopes; revoked, rotated and deregistered device identities; group membership;
  - epoch rotation; wrong cap; duplicate and absent grants;
  - the entry approval's abort;
  - the old-code version bypass refused, and migration.
- **Rust:** `make edge-test`. It decrypts ciphertexts produced by the pinned SDK ([`interfaces/seal-vectors.json`](../interfaces/seal-vectors.json)) with every 2-of-3 subset, and matches the SDK's group encodings, ElGamal, proof of possession, personal-message and request signatures. It also covers tampering, sessions, approval-transaction refusals, key server parsing/transport and the signing-service `seal` intent.
- **Frontend:** `node scripts/test_seal_frontend.cjs`, part of `make test-manifest`. Encryption happens before the only request; there is no plaintext fallback or upload after a failure; no credentials are accepted; identities have the specified layout; the AAD is bound.
- **Localnet:** `make test-seal-localnet`, in a `--network none` container against a fresh 1.81.1 localnet, with three [test key servers](../suifleet-edge/examples/seal_key_server.rs) that simulate the approvals on that chain:
  - an out-of-scope device; a granted device; a group grant and its removal;
  - plaintext refused; tampering; another namespace;
  - rotation; one and two key servers down; a refused session lifetime;
  - no confidential cache entries or key material in logs;
  - an ungranted versus a granted account through the official SDK.

  It also passes with `--upgrade-from` this branch's base on main, where the namespace differs from the call package. See [sf022-localnet.json](validation/sf022-localnet.json).
- **Public testnet** (agent wallet, `network:suifleet-testnet` locked):
  - `make live-gate-package-upgrade` passed for base `dd728f8` → this head ([sf022-package-upgrade-testnet.json](validation/sf022-package-upgrade-testnet.json));
  - `make live-seal-delivery`: Mysten's testnet key servers approved `seal_approve_device`, the daemon decrypted, verified and applied, and the receipt and report effects were read back. An ungranted account got `NoAccess` ([sf022-seal-testnet.json](validation/sf022-seal-testnet.json)). It passed again at the final edge code, `26f6529`, reusing the cached fixture and policy: device report `5sSF3GovFFNf76vF1WpKRdtag5p95Ft6ahPHXWNTGVyV`, wallet net 1,750,475 MIST ([sf022-seal-testnet-26f6529.json](validation/sf022-seal-testnet-26f6529.json)).

## Limits

- **Not run:**
  - real extension-wallet encryption and policy flows ([SF-010](https://github.com/tyler-suifleet/suifleet/issues/11));
  - physical Pi and hardware signer acceptance, including the PKCS#11 personal-message path on real tokens ([SF-031](https://github.com/tyler-suifleet/suifleet/issues/32));
  - real Walrus storage of ciphertext (local stand-in);
  - mainnet providers.
- **Not supported:**
  - committee/aggregator key servers on the edge;
  - in-browser decryption.
- **Not hidden:** sizes and plaintext digests.
- **No remote recall:** delivered keys and plaintext cannot be recalled.
- **Not a KMS/HSM or regulated-data store.** Follow [Seal's documented scope and security guidance](https://docs.sui.io/sui-stack/seal/security-best-practices).
