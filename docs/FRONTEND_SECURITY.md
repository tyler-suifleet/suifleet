# Frontend dependency remediation (SF-049)

Evidence date: 2026-10-15. Implementer: Codex, actual running model
`gpt-6.1-sol`, session `codex-security-remediation-20261015`, branch
`work/suifleet/sf-049-138a1ad7`. Existing issue: [#79](https://github.com/tyler-suifleet/suifleet/issues/79).
This dependency-only prerequisite includes none of SF-048's audit workflow/scripts.

## Reproduction and advisory review

Trivy **0.76.0** was freshly installed from Aqua's official release archive with
SHA-256 `e24b1c51293c1b15e4ba8d45bbc7891ba053f90278ec2698f3562c993e145fe7`,
using the read-only installer in the separate SF-048 worktree. The initial genuine
filesystem scan reproduced **19 HIGH/CRITICAL records (17 HIGH, 2 CRITICAL)** in
`dapp/pnpm-lock.yaml`. Rows are scanner records, not a claim of 19 independent
exploitable application paths. Primary maintainer advisories/releases linked below
were retrieved and reviewed; fixed-version lists are the scanner's branch-specific
ranges, with the selected release above all relevant fixed floors.

| Advisory | Initial package | Severity | Fixed versions reported | Applicability |
|---|---|---|---|---|
| [CVE-2026-93687](https://github.com/micromatch/braces/issues/73#issuecomment-5996574974) | braces 3.0.3 | HIGH | No published fix | Tailwind 3 content glob/build path; removed by Tailwind 4 migration, not suppressed or renamed. |
| [CVE-2026-73088](https://github.com/browserslist/browserslist/security/advisories/GHSA-73wf-gq98-2v4g) | browserslist 4.28.2 | HIGH | 4.28.7 | Build browser targeting queries/stats, not a wallet/browser runtime API; removed with old build stack. |
| [CVE-2026-73089](https://github.com/browserslist/browserslist/security/advisories/GHSA-c83g-rgw3-j3cx) | browserslist 4.28.2 | HIGH | 4.28.7 | Build browser targeting queries/stats, not a wallet/browser runtime API; removed with old build stack. |
| [CVE-2026-67213](https://github.com/ai/nanoid/releases/tag/3.3.17) | nanoid 3.3.12 | HIGH | 3.3.18, 5.1.6 | PostCSS generated IDs at build time; no app call accepts user-controlled ID sizes. |
| [CVE-2026-67214](https://github.com/ai/nanoid/releases/tag/3.3.16) | nanoid 3.3.12 | HIGH | 3.3.16, 5.1.16 | PostCSS generated IDs at build time; no app call accepts user-controlled ID sizes. |
| [CVE-2026-75604](https://github.com/vercel/next.js/security/advisories/GHSA-p293-qw3h-jr36) | next 14.2.35 | CRITICAL | 15.5.24, 16.3.3 | Framework server/build; routes prerender, but next start still exposes the server. No middleware, rewrites, image optimizer use or Server Actions in this app; RSC/server input handling remains relevant. |
| [GHSA-2xp9-vwfh-vxw4](https://github.com/vercel/next.js/security/advisories/GHSA-2xp9-vwfh-vxw4) | next 14.2.35 | CRITICAL | 15.5.24, 16.3.3 | Framework server/build; routes prerender, but next start still exposes the server. No middleware, rewrites, image optimizer use or Server Actions in this app; RSC/server input handling remains relevant. |
| [CVE-2026-44573](https://github.com/vercel/next.js/security/advisories/GHSA-36qx-fr4f-26g5) | next 14.2.35 | HIGH | 15.5.16, 16.2.5 | Framework server/build; routes prerender, but next start still exposes the server. No middleware, rewrites, image optimizer use or Server Actions in this app; RSC/server input handling remains relevant. |
| [CVE-2026-44578](https://github.com/vercel/next.js/security/advisories/GHSA-c4j6-fc7j-m34r) | next 14.2.35 | HIGH | 15.5.16, 16.2.5 | Framework server/build; routes prerender, but next start still exposes the server. No middleware, rewrites, image optimizer use or Server Actions in this app; RSC/server input handling remains relevant. |
| [CVE-2026-64641](https://github.com/vercel/next.js/security/advisories/GHSA-m99w-x7hq-7vfj) | next 14.2.35 | HIGH | 15.5.21, 16.2.11 | Framework server/build; routes prerender, but next start still exposes the server. No middleware, rewrites, image optimizer use or Server Actions in this app; RSC/server input handling remains relevant. |
| [CVE-2026-64645](https://github.com/vercel/next.js/security/advisories/GHSA-p9j2-gv94-2wf4) | next 14.2.35 | HIGH | 15.5.21, 16.2.11 | Framework server/build; routes prerender, but next start still exposes the server. No middleware, rewrites, image optimizer use or Server Actions in this app; RSC/server input handling remains relevant. |
| [CVE-2026-64649](https://github.com/vercel/next.js/security/advisories/GHSA-89xv-2m56-2m9x) | next 14.2.35 | HIGH | 15.5.21, 16.2.11 | Framework server/build; routes prerender, but next start still exposes the server. No middleware, rewrites, image optimizer use or Server Actions in this app; RSC/server input handling remains relevant. |
| [GHSA-8h8q-6873-q5fj](https://github.com/vercel/next.js/security/advisories/GHSA-8h8q-6873-q5fj) | next 14.2.35 | HIGH | 15.5.16, 16.2.5 | Framework server/build; routes prerender, but next start still exposes the server. No middleware, rewrites, image optimizer use or Server Actions in this app; RSC/server input handling remains relevant. |
| [GHSA-h25m-26qc-wcjf](https://github.com/vercel/next.js/security/advisories/GHSA-h25m-26qc-wcjf) | next 14.2.35 | HIGH | 15.0.8, 15.1.12, 15.2.9, 15.3.9, 15.4.11, 15.5.10, 15.6.0-canary.61, 16.0.11, 16.1.5 | Framework server/build; routes prerender, but next start still exposes the server. No middleware, rewrites, image optimizer use or Server Actions in this app; RSC/server input handling remains relevant. |
| [GHSA-q4gf-8mx6-v5v3](https://github.com/vercel/next.js/security/advisories/GHSA-q4gf-8mx6-v5v3) | next 14.2.35 | HIGH | 15.5.15, 16.2.3 | Framework server/build; routes prerender, but next start still exposes the server. No middleware, rewrites, image optimizer use or Server Actions in this app; RSC/server input handling remains relevant. |
| [CVE-2026-45623](https://github.com/postcss/postcss/security/advisories/GHSA-6g55-p6wh-862q) | postcss 8.4.31 | HIGH | 8.5.12 | Build CSS tooling; source-map/CSS inputs come from this checkout and dependencies. |
| [CVE-2026-73646](https://github.com/postcss/postcss/security/advisories/GHSA-r28c-9q8g-f849) | postcss 8.4.31 | HIGH | 8.5.18 | Build CSS tooling; source-map/CSS inputs come from this checkout and dependencies. |
| [CVE-2026-73646](https://github.com/postcss/postcss/security/advisories/GHSA-r28c-9q8g-f849) | postcss 8.5.13 | HIGH | 8.5.18 | Build CSS tooling; source-map/CSS inputs come from this checkout and dependencies. |
| [CVE-2026-93749](https://github.com/7rulnik/source-map-js/releases/tag/v1.2.2) | source-map-js 1.2.1 | HIGH | 1.2.2 | Build CSS tooling; source-map/CSS inputs come from this checkout and dependencies. |

## Chosen remediation and compatibility

- Next **16.4.1**, React/React DOM **19.3.1**, React types **19.3.0**:
  current supported releases, with Next's generated route types/automatic JSX
  configuration retained. Node **22.22.2** exceeds Next's 20.9 minimum.
  The app uses no synchronous request APIs requiring migration.
  [Next 16 migration guide](https://nextjs.org/docs/app/guides/upgrading/version-16).
- Tailwind **4.3.3** with matching `@tailwindcss/postcss`: removes the entire
  Tailwind 3/fast-glob/micromatch/Braces path. Braces **3.0.3** is still the latest
  published version with no fixed release; catching exceptions or a version rename
  would not satisfy a clean dependency gate. The v4 migration uses its actual
  PostCSS plugin/import, retains the explicit TypeScript theme config, and preserves
  v3 border defaults, pointer cursors and the existing 4px `rounded` utility.
  Autoprefixer is removed because v4 handles prefixing itself.
  [Tailwind migration guide](https://tailwindcss.com/docs/upgrade-guide).
- Direct PostCSS **8.5.30**; the lock contains Next's **8.5.23** and the Tailwind
  plugin's **8.5.18** too. All resolve Nano ID **3.3.20** and source-map-js **1.2.2**.
  The frozen dependency tree contains no Braces or Browserslist. No overrides,
  patches, ignore files, scanner exclusions or severity policies were added.
- `@mysten/sui` **2.36.0**, `@mysten/dapp-kit-react` **2.1.40** and
  `@mysten/dapp-kit-core` **1.6.38** remain pinned/unchanged. Wallet and deployment
  manifest semantics and client protocol versions are unchanged.
- Removed the unused `next lint` script because Next 16 removed that command;
  typecheck and all existing CI gates remain enabled.

## Verification

Passed with Node **22.22.2** / pnpm **11.0.8**:

```sh
pnpm --dir dapp install --frozen-lockfile
pnpm --dir dapp typecheck
pnpm --dir dapp build
```

The production build prerenders all seven pages (five app routes plus framework
pages). The checks below passed on both Node 24.21.0 and pinned Node 22.22.2:

```sh
node scripts/test_deployment_manifest.cjs
node scripts/test_wallet_manifest.cjs
node scripts/test_deployment_receipts.cjs
node scripts/test_frontend_queries.cjs
node scripts/test_target_groups.cjs
node scripts/test_protocol.cjs
node scripts/test_component_catalog.cjs
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_wallet_browser.cjs
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_frontend_runtime.cjs
```

These check gRPC/BCS queries and pagination/errors, transaction/receipt effects,
wallet account/network guards, 30 shared recipe cases and 17 catalog vectors.
The real Chromium fixture checks connect/sign/reject/failed effects/chain mismatch/
disconnect. The new production-server test checks all five routes, hydration,
wallet connect UI and computed layout/CSS with external requests blocked. Neither
browser test uses an extension wallet or submits a public-chain transaction.

## Final genuine scan

The same scanner/configuration was rerun after the dependency changes. Equivalent
command (PRIVATE_TMP is a mode-700 directory outside the repository; raw JSON is
mode 600 and is never uploaded):

```sh
trivy filesystem --config PRIVATE_TMP/empty.yaml \
  --secret-config PRIVATE_TMP/empty.yaml --ignorefile /dev/null \
  --include-dev-deps --scanners vuln,secret,misconfig \
  --skip-dirs '.git,**/node_modules,**/.next,**/target,.ci-cache' \
  --format json --output PRIVATE_TMP/report.json --quiet .
```

Both YAML files contain `{}`. The read-only SF-048 gate helper returned exit **0**:
**6 findings, 0 blocking**, including **zero HIGH/CRITICAL vulnerabilities, zero
secret findings and zero HIGH/CRITICAL misconfigurations**. Coverage includes
`dapp/pnpm-lock.yaml`, **both** `suifleet-edge/Cargo.lock` and
`suifleet-indexer/Cargo.lock`, `Dockerfile` and `docker/release.Dockerfile`.
Remaining findings are PostCSS 8.5.18 CVE-2026-69153 (MEDIUM), rustls 0.23.40
GHSA-2mjx-qc3c-rqvc (MEDIUM, both Rust locks), openssl 0.10.79
CVE-2026-45784 (MEDIUM, indexer), and DS-0026 (LOW, both Dockerfiles).
They are reported, not suppressed. Rust dependencies and Docker configuration
were not changed in this frontend remediation.

The 11 Python deployment-manifest tests also passed. Supplemental compiled-Move
interface checks were attempted on the host and could not start because `sui` is
not installed there; a network-isolated older cached `suifleet-dev:sf010` image
also failed `sui move summary`. These are not recorded as passes. The current-head
CI image runs the normal compiled-interface gates; no contract/schema source changed.

Current-head CI run URLs/results are recorded in the PR and ticket handoff.
No production deployment, real extension-wallet acceptance, public-network or
hardware run was performed. Existing extension/public-network and physical
integration work remains visible in [SF-010/#11](https://github.com/tyler-suifleet/suifleet/issues/11)
and [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).
This records the ticket's requested honest wallet acceptance limit; it does not
claim a new integration waiver or that those acceptance tests passed.
