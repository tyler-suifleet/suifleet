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

Raw catalog selection, manifest IDs/digests and Greengrass import remain secondary
under advanced tools/details. Catalog selection changes the destination for new
publications, while browsing continues to show all the publisher’s catalogs.

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
rejection/retry, effects-confirmation UI states, immutable duplicate detection,
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
