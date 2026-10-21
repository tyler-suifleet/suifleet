# Wallet onboarding (SF-088)

`/get-started` guides connection, deployment/network verification, gas funding,
registration and device custody transfer. The header wallet menu still supports
installed Wallet Standard extensions. dApp Kit now also registers Slush web
(default origin `https://my.slush.app`, metadata at
`https://api.slush.app/api/wallet/metadata`). An installed Slush extension takes
precedence over the web wallet according to the pinned SDK.

## Google zkLogin configuration

The provider is **Google only**. The integration pins `@mysten/enoki` 1.3.0,
with wallet-standard 0.21.34 and webcrypto-signer 0.2.34, compatible with the
existing Sui 2.36.0 SDK. It uses the Wallet Standard initializer, Enoki’s hosted
salt and proving service at `https://api.enoki.mystenlabs.com`, and Google OAuth
at `https://accounts.google.com/o/oauth2/v2/auth`. No SuiFleet login server,
private Enoki API key, OAuth client secret, custom prover or custom salt service
is required. No transaction sponsorship is enabled by this integration.

Provision an Enoki app in the Enoki portal, enable its Google provider and obtain
its **public** API key. Configure a Google web OAuth client with the deployment’s
exact allowed origin and redirect URI ending in `/get-started`. SuiFleet pins
that same-origin callback so login from the header on any page returns to the
first-run route in the popup. Use a fixed HTTPS deployment origin for real
acceptance; allow loopback development explicitly where supported.
Enoki origin/network restrictions and
Google redirect allowlists must match the chosen deployment. Review third-party
service availability, billing/quota and privacy terms before enabling it.

Set these **public build-time identifiers** in the untracked `dapp/.env.local`
or hosting build environment, then rebuild:

```dotenv
NEXT_PUBLIC_ENOKI_API_KEY=enoki_public_...
NEXT_PUBLIC_GOOGLE_CLIENT_ID=....apps.googleusercontent.com
```

Never put private keys, OAuth secrets, JWTs, proofs, device keys or recovery phrases
in source, issue evidence or logs. The client rejects a private-key-shaped Enoki
configuration and leaves Slush/extensions available when public configuration is
missing. Google is enabled only on testnet/mainnet; Enoki does not support
localnet. Localnet uses an appropriate extension/fixture wallet. Do not represent
fixture tests as a real Google login on a local chain.

## Sessions and recovery

The pinned Enoki Wallet Standard implementation generates non-extractable P-256
WebCrypto ephemeral keys and stores CryptoKey objects in IndexedDB, rather than
serializing private key bytes into local/session storage. Enoki stores encrypted
session data in IndexedDB; its public-app-key-based encryption is not a boundary
against same-origin JavaScript. JWTs and proofs must remain private. Protect the
origin against script injection, avoid untrusted third-party scripts and disconnect
on shared devices. This is software browser custody, not hardware key isolation.
SuiFleet adds no key or token persistence.

`additionalEpochs: 0` limits a nonce to the current epoch. Before every fleet write,
SuiFleet checks the Enoki session wall-clock expiry and maxEpoch against a fresh
node service-info epoch, after deployment verification. Missing/expired/unverifiable
sessions fail before signing and instruct the owner to disconnect and sign in
again. Chain verification, account/network race checks and independent transaction
effects confirmation apply equally to Google, Slush and extension wallets.

The address depends on the Google account, OAuth audience and Enoki app salt.
Keep account recovery and MFA current. Losing the account or access to its salt
service can prevent recovery; creating a different app/client/salt can produce a
different address. SuiFleet cannot reset ownership. While access remains, use the
get-started transfer form to transfer each activated device’s DeviceOwnerCap to a
verified extension address or initialized multisig. This preserves the existing
contract authority/revocation and old-package isolation boundary. It does not
transfer group administrator fields, historical deployment creators, gas coins or
other assets. Recreate groups and desired deployments under the new owner and
review old deployment/role permissions separately. Recommend hardware-backed or
multisig custody for business fleets before large deposits.

## First run and SF-083 extension

The page shows the manifest network, connected address and verified balance. A
nonzero gas balance enables registration; it does not promise sufficient gas.
The wallet estimates the actual cost. Fund using a same-network SUI transfer or a
disposable localnet/testnet faucet. No card checkout or prepaid budget exists here.
SF-083 inserts its estimate/card-funding/budget-deposit steps at the marked
extension point between the gas check and registration in `FirstRun.tsx`; it must
retain network/account checks and distinguish gas from a fleet budget.

Registration uses the existing single PTB to register, issue the device cap and
activate custody. Confirmed effects produce a digest and an inventory refresh;
daemon provisioning/online health remains separate. Errors explain gas charges
and ambiguous submission: inspect wallet history and refresh inventory before
retrying. Do not infer a healthy device from a successful registration.

## Verification and outstanding acceptance

```bash
node scripts/test_onboarding.cjs
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_onboarding_browser.cjs
FLEET_TEST_NETWORK=testnet CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_onboarding_browser.cjs
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_wallet_browser.cjs
(cd dapp && pnpm typecheck && pnpm build)
```

The unit test is included in `make test-manifest`/`make dapp-typecheck`. Browser
tests use actual dApp Kit and the production wallet initializer/page component,
with fixture Wallet Standard signing, network responses and Enoki session metadata.
They test localnet/testnet configuration, Google registration only on testnet,
Slush registration, zero-gas gating, registration confirmation/rejection, session
wall-clock and epoch expiry, network mismatch and disconnect. They do not log
into Google/Slush, operate an extension or submit to a chain.

The user-authorized implementation/acceptance split now covers SF-088; see
[the acceptance policy](IMPLEMENTATION_ACCEPTANCE.md). Final provisioned
Google/Enoki, real Slush web and extension acceptance remains unrun and tracked
in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159). This includes
rejection, expiry, mismatch, registration effects/inventory and owner transfer to
the destination wallet or multisig. Record browser/wallet versions, origins,
public network/package IDs and transaction digests without credentials in the
final test phase. Source/security review, local failure-path/browser fixtures,
normal automatic public-testnet validation and current CI remain implementation
gates. Fixture evidence does not establish real provider or wallet acceptance.

References: [Enoki registration](https://docs.enoki.mystenlabs.com/ts-sdk/register),
[Enoki sign-in](https://docs.enoki.mystenlabs.com/ts-sdk/sign-in),
[dApp Kit configuration](https://sdk.mystenlabs.com/dapp-kit/dapp-kit-instance).
