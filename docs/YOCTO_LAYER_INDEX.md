# OpenEmbedded Layer Index submission

Scarthgap and Wrynose are the initial listing candidates. The submission owner
is the [SF-295 publication owner](https://github.com/tyler-suifleet/suifleet/issues/820).
[SF-312](https://github.com/tyler-suifleet/suifleet/issues/838) records final
evidence and the submission and indexing states. Both states are pending.
Layer Index listing and Yocto Project Compatible certification are separate.

## Reproduce the readiness report

Run from the core checkout with Python 3.10+ and Git:

```sh
python3 scripts/check_yocto_layer_index.py
```

The reporter fetches each public series branch over HTTPS in a temporary Git
repository with credential helpers and prompting disabled. It checks the
committed root layer configuration, dependency collections, series declaration,
README maintainer and contribution instructions, MIT license, and the three
recipe files. It compares each fetched SHA to the recorded build and layer-check
revisions, and includes the [evidence ledger](validation/yocto-layer-index.json)
links. Exit 0 means candidate branch heads match the recorded build revisions
and pass the metadata checks. The report identifies layer-check revision
matches, pending final inputs and Kirkstone results independently.

The 2026-11-27 anonymous fetch returned
`c78e6297691b38da87989a7ec279734a6669e379` for Scarthgap, Wrynose and
Kirkstone. The [SF-295 validation receipt](https://github.com/tyler-suifleet/suifleet/issues/820#issuecomment-7169878579)
records the source-archive fetch/unpack, layer-check, standalone package tasks,
QA, package inspection and AArch64 user-mode smoke with exact OE, BitBake, and
Rust pins. Wrynose and Kirkstone ran the recipe-specific compile, QA and
IPK tasks after default queues stopped during generic QEMU kernel rebuilds.
Both pinned source archives were downloaded anonymously again on 2026-11-27
and matched the ledger's SHA-256 values. To repeat the archive check, download
each immutable URL in the ledger with `curl -q -fL` and compare
`sha256sum` output against the recorded digest. AArch64 user-mode smoke uses the host
kernel. Earlier [guest boot evidence](YOCTO_STANDALONE_VALIDATION.md)
identifies its own source and guest kernel. [Kirkstone evidence](YOCTO_LEGACY_SERIES.md)
covers its separate older-kernel and signer limits. Physical acceptance is tracked
in [SF-031](https://github.com/tyler-suifleet/suifleet/issues/32).

On 2026-11-27, the full Scarthgap `yocto-check-layer` run against the
anonymous public checkout at `c78e6297691b38da87989a7ec279734a6669e379`
returned exit 0 and `PASS` with BitBake 2.8.1 on WSL2 kernel 6.6.87.2.
It ran eight tests in 554.149 seconds with two BSP/distro skips and one
unexpected success in the upstream expected-failure patch-status check.
The baseline signature comparison and both world checks passed. This
supersedes the layer check on `7831cebe241050fc2bdb319786144c3f4d62edcb`,
which differs from the public revision only in README and export provenance.
The earlier standalone build and runtime receipts remain separate.
[Scarthgap verification in PR #1087](https://github.com/tyler-suifleet/suifleet/pull/1087)

Fresh standalone qemuarm64 build directories on 2026-11-26 parsed the public
revision without preseeded codec artifacts. `bitbake -p` completed with zero
errors on Scarthgap (BitBake 2.8.1, 1,884 recipes) and Wrynose (BitBake 2.18.0,
2,004 recipes). Both reported the BitBake WSL host advisory. A first harness
attempt on each stopped before parsing because it had cleared the hash server
setting while using OEEquivHash. The successful runs used a local automatic
hash server. This parse run used the SF-295 dependency pins and no integration
layer. SF-295 package outputs were smoke-tested again with QEMU AArch64
user-mode 6.2.0 on WSL2 kernel 6.6.87.2. On both candidates, provisioning and
address derivation agreed, packaged Node reported 22.22.2, the codec files
matched their pins, and the packages contained no runtime identity or journal.

Before final readiness, confirm the evidence from the
[SF-306 source audit](https://github.com/tyler-suifleet/suifleet/issues/832),
[SF-307 optional-runtime packaging](https://github.com/tyler-suifleet/suifleet/issues/833)
and [SF-311 customer guide](https://github.com/tyler-suifleet/suifleet/issues/836)
against each candidate's exact public branch SHA. The 2026-11-27 tracker
check found SF-306 open and SF-307 and SF-311 closed as completed. Keep all
three as final evidence inputs. Recheck the candidate heads
after evidence collection and immediately before submission. Update the ledger
with the immutable source and receipt links. For each candidate, use a fresh
standalone build directory with the corresponding pinned core, meta-oe and
documented Rust provider from the [kas profiles](../kas). Use qemuarm64 and the
documented distro requirements. Keep the integration layer out of BBLAYERS.

- Run `bitbake -p` and `bitbake-layers show-recipes 'suifleet*'` with default
  archive settings and without preseeded codec artifacts. Confirm the documented
  unsupported-host behavior as part of SF-307.
- In the fresh baseline, put only the core/distro and required toolchain
  layers in BBLAYERS. For Scarthgap use Poky `meta`, `meta-poky` and the
  Scarthgap Rust mixin, with the exact revisions and distro settings from
  [the Scarthgap packaging profile](../kas/packaging.yml). Use
  `MACHINE = "qemuarm64"`, an isolated `DL_DIR` and `SSTATE_DIR`, empty
  `SSTATE_MIRRORS`, and `BB_HASHSERVE = "auto"`. Check the anonymous
  checkout's SHA before running:

  ```sh
  yocto-check-layer <public-layer> --dependency <meta-oe> --no-auto-dependency
  ```

  Pass the pinned `meta-oe` directory as the dependency. Leave the candidate
  out of the baseline so the upstream tool can compare world signatures
  before and after adding it. Keep layer defaults and signature checks enabled.
- Fetch and unpack the daemon and codec from the pinned sources, build
  `suifleet suifleet-node suifleet-walrus-codec`, and record task execution,
  package QA, checksums and tooling versions. Use the layer's opt-in
  `suifleet-core-fixes` when called for by the standalone packaging guidance.
  Inspect packages for pinned build info and absence of device identity and
  journals. Record the supported runtime smoke and its kernel scope.

Kirkstone can be added later after its final verification. Its current branch
and evidence do not expand the initial submission. Dunfell remains outside the
listing plan.

## Draft form values

Use the [public submission form](https://layers.openembedded.org/layerindex/submit/)
after the evidence gates. The public form creates a record for the Index
development branch (`master`). The repository's default development ref is
`main`. Before sending, coordinate with Index maintainers to stage the named
release rows without advertising unvalidated development-branch compatibility.
For any requested development row, verify `main` against the Index development
baseline first. Ask for supported release rows if development compatibility
cannot be established. Avoid duplicate submissions.

| Field | Value |
| --- | --- |
| Layer name | `meta-suifleet` |
| Layer type | Software |
| Summary | SuiFleet Linux edge client and optional Walrus archive runtime |
| Description | OpenEmbedded recipes for the SuiFleet edge daemon, isolated signing service, first-boot provisioning, and optional AArch64 glibc Walrus archive runtime. Initial release rows requested for Scarthgap and Wrynose. See the README for pinned Rust toolchains and distro requirements. |
| Repository URL | `https://github.com/tyler-suifleet/meta-suifleet.git` |
| Repository subdirectory | Leave empty for the repository root |
| Actual branch | `main` for a verified development row. `scarthgap` and `wrynose` for their respective release rows. |
| Repository web interface URL | `https://github.com/tyler-suifleet/meta-suifleet` |
| Repository web interface tree base URL | `https://github.com/tyler-suifleet/meta-suifleet/tree/%branch%[/%path%]` |
| Repository web interface file base URL | `https://github.com/tyler-suifleet/meta-suifleet/blob/%branch%/%path%` |
| Repository web interface commit URL | `https://github.com/tyler-suifleet/meta-suifleet/commit/%hash%` |
| Usage web page URL | `README.md` |
| Mailing list URL | Leave empty |
| Dependencies | `openembedded-core` and `meta-oe`, corresponding to `core` and `openembedded-layer` |
| Maintainer | Tyler Bush, `tyler.jonathan.bush@gmail.com`, layer maintenance |
| Yocto Project Compatible version | Leave empty |

Scarthgap's Rust mixin is a documented build input and is not a
`LAYERDEPENDS` collection in the layer. The [form implementation](https://github.com/yoctoproject/layerindex-web/blob/master/layerindex/forms.py)
and [URL handling](https://github.com/yoctoproject/layerindex-web/blob/master/layerindex/models.py)
define the field names and URL tokens.

## Submission and processing records

The SF-295 owner completes required human verification (CAPTCHA for anonymous
submission) and records the submission date, submitted branch SHA(s) and
confirmation or review URL in SF-312. Keep any private confirmation details
local. The pending moderation step belongs to the OpenEmbedded Index maintainers.

After moderation, open the public `/layerindex/branch/<series>/layer/meta-suifleet/`
page for each release row. Verify repository, root, branch checkout, dependency
and maintainer fields. Check that the successful update processed the intended
revision and that `suifleet`, `suifleet-node` and `suifleet-walrus-codec` appear
for the series. Record the public layer and recipe URLs, processed revision,
check date and any moderator or parse blocker separately from the submission
receipt. On 2026-11-27 the Scarthgap and Wrynose public layer URLs returned
HTTP 404. The `openembedded-core` and `meta-oe` rows on both series returned
HTTP 200.
