# Component software library (SF-103)

Components opens the connected publisher’s software library, across all their
catalogs. Search by component name, version or platform. Each named component
lists immutable versions, platforms in selection order, observed recipe
confidentiality and current Walrus recipe/artifact reachability. Loading,
disconnected, empty, failed-query and no-search-result states are explicit.
A confidential recipe hides its artifact reference; artifact availability stays
private/unknown until authorized decryption. Unknown or invalid recipe content
must not be labeled public. These probes do not authenticate downloaded software
or establish installation, execution, future retention or decryption authorization.
The daemon still authenticates recipe and artifact digests before execution.

Use **Publish version** to:

1. Set up a publisher catalog with a confirmed wallet transaction if needed.
2. Choose a stable component name and a new semantic version.
3. Select the first platform and artifact. Add, remove or reorder additional
   platforms. Devices select the first compatible entry; put wildcard fallbacks
   last. Optional installer commands and configuration/validation are expandable.
4. Select confidential delivery and an administered access policy, or explicitly
   acknowledge public delivery. Confidential delivery requires configured Seal
   servers and never falls back to plaintext.
5. Review the immutable version, ordered files, commands and delivery choice.
   Upload progress comes from browser upload events; bytes sent is distinct from
   Walrus processing. Then verify the network, confirm in the wallet and wait for
   successful execution effects from the fleet node.

The publisher freezes inputs while running and guards same-render duplicate
clicks. A confirmed publication cannot be submitted again from the success view;
the catalog also enforces immutable name/version registration. Changing or
removing the publishing account during upload prevents that operation from
registering under a successor account. Failed publication leaves uploaded blobs
on Walrus, so check the library before explicitly retrying. A failed confirmation
is not evidence that a submitted transaction never executed.

**Deploy this version** opens the published version’s target selection and review,
without uploading again or submitting a transaction. The review shows platform
order and requires an explicit deployment action. Publishing never targets a
device; deployment does not establish installed/running state without device
reports. Platform tools own OS installation, reboot and firmware recovery; no
systemd, Yocto or partition layout is assumed.

Additional catalog creation/selection, manifest IDs/digests and Greengrass import remain secondary
under advanced tools/details. Catalog selection changes the destination for new
publications, while browsing continues to show all the publisher’s catalogs.

A name and version must be unique across the whole library, not only within the
selected catalog. The contract keys versions by `name@version` inside one catalog
and cannot see the publisher's other catalogs, so the publisher and Greengrass
import check every catalog shown in the library before uploading or signing. A
match blocks publication and shows the existing entry, its catalog and a link to
deploy it. Versions published before this check may repeat a name and version in
two catalogs; the library labels each such entry with its catalog. A wallet or
script that calls the contract directly is not covered by this client check.

## Regression checks

Use the locked frontend dependencies and pinned Node/pnpm baseline from
[FRONTEND_CLIENT.md](FRONTEND_CLIENT.md):

```bash
make dapp-typecheck
make dapp-build
# Requires the build above: the browser regression uses its actual CSS.
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_components_browser.cjs
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_wallet_browser.cjs
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_upload_browser.cjs
node scripts/test_component_catalog.cjs
```

The Components Chrome harness renders the actual Components and deployment
pages, validators, transaction builders and upload code. Chain discovery,
Wallet Standard submission and storage responses are controlled fixtures; it
uses no public network or production credentials. It covers loading/disconnect,
setup rejection, library errors/search, guided multi-platform ordering, upload
progress/failure, confidential fail-closed behavior, duplicate clicks, wallet
rejection/retry, effects-confirmation UI states, same- and cross-catalog
duplicate detection (publisher and import) with the existing entry shown,
catalog labels on repeated library entries,
accessible control names, mobile reflow and deployment navigation without writes.
The separate wallet regression exercises the actual application wallet hook and
independent effects checking. Seal tests in `make dapp-typecheck` exercise real
encryption and reject plaintext fallback, malformed recipes and expired storage.

Final production-provider and manual real-wallet acceptance remain explicitly
unrun in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159), under the
[user-authorized implementation split](IMPLEMENTATION_ACCEPTANCE.md).
Physical device and actual-mainnet acceptance are deferred there too. Browser
fixtures do not establish those external results. Normal CI, applicable automatic
testnet validation and fresh independent review remain required.

## Release storage period (SF-165)

Publishing, Greengrass import and the Deploy upload show a period in days, a
live Walrus storage/write estimate for the selected artifact bytes, and the
projected calendar expiry before upload/signing. The default uses the selected
fleet's configured retention epochs when present, otherwise the build default.
Days round up to whole Walrus epochs using the live staking schedule; the live
storage horizon bounds the choice. Recipe/encryption overhead and transaction
gas are additional, explicitly labeled. Public publisher sponsorship is labeled
as such: it is not a WAL charge to the connected wallet. Newly created objects
are sent to that wallet; reused content may belong to another owner. A publisher
response that does not cover the chosen period prevents version registration.

Fleet-paid uploads retain the existing finite-retention authorization and reserve
checks. A longer prepaid pool period is preserved. If necessary, a confirmed
owner-authorized extension buys the requested additional retention from fleet
WAL before publication; it never falls back to the public publisher. Normal
managed-fleet transactions borrow and return the custodied budget cap through
the existing wallet transaction boundary.

Library platforms, published-version deployment review and deployment history
(including sharded catalog bindings) show current projected expiry and warn
within seven days or after expiry. Current chain-owned objects determine renewal
authority, rather than browser receipts. **Extend storage** shows additional days,
WAL estimate, gas notice and projected date, refreshes ownership/horizon, then
requires successful wallet/node effects before showing confirmation. Unknown,
expired, foreign-owned or unavailable storage stays explicit. Epoch-duration
changes can move projected dates. Extending storage does not deploy software or
prove installation or boot health.

A confidential recipe hides its artifact reference. **Show confidential artifact
storage** uses an explicit five-minute signed Seal session for an account granted
access by the release policy. It checks network/account/view, namespace, provider
set/threshold, recipe AAD and the immutable recipe digest; downloaded recipes are
bounded to 1 MiB and decrypted bytes are wiped. Only the artifact reference is
kept in view memory. Policy denial, provider outages and expired/mismatched
sessions do not reveal the reference or fall back to plaintext. Legacy deployments
without an authenticated recipe digest remain explicitly unknown. No session,
key or decrypted recipe is persisted. Upload success also exposes each newly
stored blob's renewal action directly, including confidential artifacts.

Offline period/price/warning/transaction tests run with `make dapp-typecheck`.
The Chrome Components regression changes the period and checks it reaches every
artifact/recipe request; the upload regression checks explicit quote review.
[The dedicated-testnet receipt](validation/sf165-testnet.json) records a 12-day
publication, a two-day wallet extension (epoch 575 to 577), successful effects,
artifact readback, exact WAL/SUI charges and the same non-replenishing 0.15 SUI
allowance across harness repairs. This exercises the actual application builders
with CLI signing; manual real-wallet/production-provider, mainnet and physical
acceptance remain unrun in SF-031/#32 and SF-087/#159.
