# Access policies

Access policies control **decryption of confidential releases**, separately from
publish, deploy, device ownership and administrator permissions. `/access` always
explains this purpose and the creation → recipient grants → confidential upload
workflow, including when disconnected or using a deployment without Seal.

Connect the wallet holding an `AccessAdminCap` to manage its policy. Create a
recognizable name, then choose an activated named device, a group you administer,
or a complete Sui account address. Device choices omit legacy devices without
an owner capability; the contract remains authoritative for current key status.
Group grants delegate recipient selection to the group administrator through
current membership. Account grants let the address request keys directly. IDs
are secondary in choices and policy technical details. Granting a policy does not
apply it to existing releases: select it in **Deploy component** (`/firmware`)
when uploading a new confidential artifact and recipe.

Loading, read failures (with retry), missing encryption key servers, no owned
policies and empty device/group choices have explicit states and next actions.
Rejected create/grant transactions preserve inputs. Wallet transactions still use
`useFleetTransaction`: deployment/network/account verification and both wallet and
node effects must succeed. A digest alone is insufficient. The page does not
introduce a plaintext fallback or bypass any contract check.

Revocation stops future key release after servers observe the change. It cannot
recall fetched keys, cached keys or plaintext, or uninstall delivered software.
Rotation retires all earlier key epochs for new requests. Re-encrypt and redeploy
any release that devices still need. See [confidential delivery](CONFIDENTIAL_DELIVERY.md)
for cache lifetimes, device identity revocation and provider limitations.

## Separate Seal testnet profile

[`profiles/seal-testnet`](../profiles/seal-testnet/) contains a **separate disposable
fleet**, not an upgrade or replacement of the existing public OTA registry:

- `deployment.json`: current-client manifest resolved and verified from testnet;
- `seal.json`: two public key server IDs, threshold 2 and expected UpgradeCap owner;
- `frontend.env`: matching public build settings, with no provider credentials;
- `publication.json`: pinned toolchain, bytecode verification, transaction effects
  and cumulative spending, including the failed first attempt;
- `verification.json`: dated read-only chain, authority and provider proof evidence.

Reverify before using this profile, since a network reset, upgrade or unavailable
provider invalidates earlier evidence:

```bash
node scripts/verify_seal_profile.cjs
```

The script makes no transactions and installs no settings. It checks the current
frontend schema/client range, fresh chain identity, package/version/type origins,
latest UpgradeCap and owner, shared registry, key server types, HTTPS proof of
possession, and agreement of `frontend.env` with the manifest and server settings.
It fails rather than reusing stale proof. Reuse this published profile while valid;
do not republish identical contracts for a frontend rebuild.

To preview it, use a **separate disposable checkout** of the reviewed branch.
Install locked frontend dependencies there, explicitly copy
`profiles/seal-testnet/frontend.env` to that checkout's `dapp/.env.local`, and build
with `make dapp-build` (or `cd dapp && pnpm build` in the development environment).
Serve that checkout's static `dapp/out` at a separate loopback origin/port under
its resource lock. This does not copy devices, policies, releases or permissions
from the public fleet. Do not copy these settings over the existing live UI or
activate a daemon with another registry's configuration. A new profile has no
policy or enrolled devices until explicitly created there.

The dedicated testnet agent owns the profile's UpgradeCap; policy administration
belongs to the wallet that creates each policy. No private key, wallet credential
or provider API credential is included. Testnet key servers provide no durability
or SLA guarantee. This profile establishes configuration compatibility, **not**
provider production readiness or encrypted delivery acceptance.

## Verification and remaining acceptance

```bash
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_access_browser.cjs
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_wallet_browser.cjs
node scripts/test_seal_frontend.cjs
make dapp-typecheck
make dapp-build
```

The Access browser regression renders the actual page, styles and transaction
builders in Chrome. Wallet/query/execute hooks are explicit fixtures; it covers
disconnected, unsupported, missing servers, loading/error/retry/empty states,
named create/device/group/account grants, duplicate/invalid input, rejected input
preservation, revoke, rotation confirmation, pending controls and 375/1280px widths.
The separate wallet browser regression exercises the real dApp Kit and effects
boundary with a Wallet Standard fixture. Neither is a real extension session.

Fresh independent review and exact-head CI/testnet status remain merge gates.
Encrypted allowed/denied/revoked delivery with this profile remains in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32); real extension
sessions, production providers, physical targets and mainnet remain unrun in
[#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159). Follow the
[implementation acceptance split](IMPLEMENTATION_ACCEPTANCE.md); no deferred
check is reported passed by preparing this profile.
