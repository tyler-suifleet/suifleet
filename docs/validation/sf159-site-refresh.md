# SF-159: refreshed testnet frontend and mobile preview

2026-10-27 UTC. Frontend: merged main `5a782d05`. Publisher repair: `e94439cc`.

The existing testnet Site was retained:
`0xb1891eab584649b8b19c08fa16fda71351b973bfafef3ab1209874bbd79c7ab4`.
The current export uses the same verified testnet core deployment as the user's
local UI, including its existing optional-feature limitations. No core contract
upgrade, account migration or Pi update occurred.

The first update's effects succeeded, but readback failed because unchanged assets
still referenced the expired older blob `AgTSbe-i-ucJSuI5mz0ogF-UbkelgzbgRHfGoGwpofQ`.
The repair maps every exported resource to the fresh certified packed blob, even
when its content hash is unchanged. An explicit `--operation refresh` independently
checked the old Site transaction's successful effects, archived the original plan
and error, used a distinct intent label and retained all publisher reservations.
Unknown or failed intents cannot be replaced by this recovery.

The first repair attempt refused an absent system-state epoch before another Site
transaction. The pinned SDK keeps that clock in staking state. The corrected
pretransaction check rejects uncertified or expired packed blobs without buying
another identical blob or attempting to update the Site with expired storage.

Final live result: **129 resources and 31 routes verified**, including exact
on-chain resource metadata, full Walrus byte readback, hashes, byte ranges,
headers, routing, ownership, effects and independent historical gas reconciliation.
The new blob was reused for recovery. The publisher identity and the existing Site
were preserved; its lifetime non-replenishing limit increased once from 0.7 to
1 testnet SUI through a verified 0.3 SUI transfer. The transfer remains publisher
capital, not a fee. Four publication/recovery transactions cost 0.048059841 SUI
net; funding cost 0.001988 SUI gas. Known SUI fees total 0.050047841 SUI. WAL
storage fees came from existing publisher funds and are not quantified here.
The new blob is certified in Walrus epoch 563, expiring at 566. No Mainnet or
customer wallet was used. The shared testnet lease was released after verification.
Public digests, gas effects and archived-plan hash: [JSON receipt](sf159-site-refresh.json).
Private journals, wallet material, signed intent bytes and runtime paths are not
included. No pending publication plan remains.

## Phone access

Walrus Foundation's public portal does not serve testnet sites. A separate
Cloudflare Quick Tunnel exposes a static in-memory mirror of the exact verified
export over HTTPS; this is **not** a permanent public Walrus testnet portal.
It depends on this WSL machine and the two user services remaining running;
restarting the tunnel can assign another hostname. Public assets are restricted
to the build receipt; directory browsing, private files and ws-resources.json
are not served. The cloudflared release binary was checksum-verified against
Cloudflare's GitHub release asset digest. No account or production credentials
were provisioned.

`https://hazardous-archived-minolta-mails.trycloudflare.com`

Normal public HTTPS returns 200. A disposable Chrome mobile viewport (390px)
verified a secure context, no page overflow or JavaScript errors, a wallet dialog
with a Slush option, and HTTP 404 for publication metadata. The first DNS lookup
briefly failed; subsequent normal DNS and HTTPS requests succeeded. This is
browser UI validation, **not** physical-phone Slush signing acceptance. No wallet
approval or transaction was performed in that browser.
See [browser receipt](sf159-https-preview.json).

## Offline validation

- `python3 -m unittest discover -s scripts/tests -p test_walrus_site.py -v`:
  7 passed, including refresh boundary changes, unknown/failed/missing intents,
  mismatched/missing effects and distinct-operation enforcement.
- `node scripts/test_walrus_site_protocol.cjs`: passed existing pinned ABI,
  packing/ranges, hash byte order, path/symlink and allowance checks plus
  unchanged-asset refresh, removals, duplicate refusal, original-evidence
  preservation and certified/unexpired packed-blob preflight.
- `python3 -m py_compile scripts/live_walrus_site.py` and `git diff --check`: passed.

The site export was built from latest merged frontend source with locked
frontend dependencies using the checksum-verified pinned Site builder. Existing
release and wallet/environment checks stayed enabled. The publisher fix still
requires normal independent review and CI before merging.
