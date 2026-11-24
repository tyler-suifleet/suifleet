# Standalone Yocto layer readiness evidence (SF-230)

These are software build/export results,
not physical, production-provider, mainnet or public-distribution acceptance.

## Export and offline checks

`INSIDE_DEV_CONTAINER=1 make test-yocto-packaging` passed all 44 tests
(15 image-inspection, 26 runtime-packaging, 3 export tests). Export tests verify
byte reproducibility, committed rather than modified working bytes, exclusion of
unlisted identity material, safe paths, symlink refusal, output no-overwrite and
an inventory containing all software-layer files without integration policy.
Relative Markdown file links in the changed packaging guide and component guide
were checked against the checkout; `git diff --check` passed.

Two runs of `python3 scripts/export_yocto_layer.py --revision
a77b8bad90de12eb1cfc2ac62dff0e4ae5c1aa1f --output <distinct-output>` produced
identical 22-member tar.gz files, SHA-256
`fdc74be3198d23b5ce6c353b113abc84adb036ceb758ac20d87eaf8c2a55403d`.
All 21 file digests were independently checked against export-provenance.json.
This hash identifies the historical layer snapshot at that revision; subsequent
commits update evidence, the relocated test-component guide and exported
README/RELEASE validation qualifications. It is not the final export hash.
The archive is a local readiness artifact, not a published download.

Both kas profiles expanded with kas 4.7 using:

```
kas dump --skip repo_setup_loop --skip finish_setup_repos --skip repos_checkout kas/packaging.yml
kas dump --skip repo_setup_loop --skip finish_setup_repos --skip repos_checkout kas/packaging-wrynose.yml
```

These commands expand configuration without fetching/checking out layers. Both
include the software and integration layers; external full revision pins are
unchanged. Anonymous HTTPS download of the pinned Cargo.lock returned SHA-256
`310102d73d7644815fe05df24917c332136836d64dcff5f7ca35b44c63b2d937`.
`gh repo view tyler-suifleet/suifleet --json isPrivate` confirmed public visibility.
Neither check establishes independent source-artifact publication.

## Exported software-layer compatibility

The actual extracted archive at `/tmp/sf230-export/meta-suifleet` was tested,
without the integration layer. For each release, an isolated build directory
used the pinned core (and Scarthgap Rust mixin), systemd/usrmerge, qemuarm64,
existing download/sstate caches and the existing five pinned codec artifacts:

```
umask 022
source <pinned-core>/oe-init-build-env /tmp/sf230-check-<series>
yocto-check-layer /tmp/sf230-export/meta-suifleet \
  --dependency <pinned-meta-openembedded>/meta-oe --no-auto-dependency
```

The dependency argument resolves meta-oe without testing that upstream layer.
Software-layer signature checks stayed enabled. Stock Scarthgap returned exit 0,
`PASS`, 8 tests, 2 BSP/distro skips and 1 unexpected success: its unmodified
upstream patch-status test is decorated expectedFailure, but the layer has no
patches and the assertion succeeds. This qualification is retained, not hidden.
Stock Wrynose returned exit 0, `PASS`, 9 tests and 2 BSP/distro skips, including
its security-policy check. Both passed parse, README, environment, unchanged
baseline signatures, world and world-with-check-layer-class checks. These are
not permission to use the optional Yocto Project Compatible branding.

## Recipe builds

Builds use the full core/toolchain/layer pins from the two committed kas profiles,
existing external build directories, umask 022 and the current checkout's software
plus integration layers in BBLAYERS. No upstream layer source was modified.
These successful builds therefore include the integration layer's Scarthgap
OpenSSL install serialization (`PARALLEL_MAKEINST = "-j1"`), Wrynose libcap
target-toolchain relink fixes, Wrynose xzmore Bash interpreter/runtime-dependency
fixes, and AArch64 glibc usrmerge loader/linker-script guard. The standalone
export contains none of these appends. No recipe task-execution build from the
extracted export without the integration layer was run on either series. Its
yocto-check-layer passes above do not establish that build path. Standalone
consumers must supply equivalent distro fixes/guards and validate them, or accept
unvalidated status and possible build failures.

Codec inputs are the verified artifacts already present at
`/home/tyler/dev/codex/yocto-sf120/build/suifleet-walrus-codec/2d231cadf3c0c8e58b48dc9f48dd7890faaafa1f`.

Scarthgap, Raspberry Pi Cortex-A53 package target, rustc 1.98.1:

```
source /home/tyler/dev/suifleet-yocto/poky/oe-init-build-env /home/tyler/dev/codex/yocto-sf120/build
bitbake suifleet suifleet-node suifleet-walrus-codec suifleet-test-component
```

Exit 0; 2051 tasks, 2020 reused, all succeeded; daemon package QA passed. One
existing local hash-equivalence/sstate-mirror warning. The stripped AArch64 PIE
package SHA-256 is `cad45ac83282b8616cde4a5584b5c40221a8249b91f2219606d29208b0e1deb2`.
Build-info records source 2d231cad, the unchanged lock digest and crate 0.2.1.
`bitbake -n suifleet-pi-image` also returned exit 0 and resolved 4871 tasks;
this is a dependency-graph dry run, not a rebuilt image.
Package inspection found no live device.key, config.toml, signer.toml or journal.

Wrynose, QEMU Cortex-A57 package target, rustc 1.94.1:

```
export PATH=/home/tyler/dev/codex/yocto-sf025-wrynose/host-venv/bin:$PATH
source /home/tyler/dev/codex/yocto-sf025-wrynose/openembedded-core/oe-init-build-env /home/tyler/dev/codex/yocto-sf025-wrynose/build-qemu
bitbake -R /tmp/sf230-wrynose-override.conf suifleet suifleet-node suifleet-walrus-codec suifleet-test-component
```

The override sets BBLAYERS to pinned OE-Core/meta-oe/meta-raspberrypi and this
checkout’s two layers, SUIFLEET_WALRUS_CODEC_DIR to the above artifact directory,
and BBMULTICONFIG to empty (only this QEMU package target). Local concurrency
is four tasks with -j4 compiler jobs; release/source pins are unchanged.
Exit 0; 2183 tasks, 2135 reused, all succeeded. Daemon, Node and codec package
QA passed. The stripped AArch64 PIE package SHA-256 is
`37b29a0dc097f2b66a3800f5ec1d34b4928ec715d860675f5b562ae9cc049bdd`.
Build-info matches the same source/lock/crate pins. Package inspection found
no live device.key, config.toml, signer.toml or journal.

Earlier Wrynose attempts failed because retained stamps referenced absent glibc
and GCC source directories in the reused build tree. The repairs, all exit 0,
were scoped to those build inputs, with the same override:

```
bitbake -R /tmp/sf230-wrynose-override.conf -c clean glibc
bitbake -R /tmp/sf230-wrynose-override.conf -c clean gcc-source-15.3.0
bitbake -R /tmp/sf230-wrynose-override.conf -c preconfigure gcc-source-15.3.0
```

The final successful build followed these repairs; failing attempts are not
counted as passes. Initial harness attempts also corrected restrictive umask,
a late BBLAYERS override (which did not select new recipe paths), and a Wrynose
check-directory configuration using a renamed Scarthgap variable. Final builds
verified recipe paths from this branch; the initial old-path run is excluded.
No QA/sanity/signature checks were disabled. Both shared build configurations
were restored and their resource locks released after testing.


## Opt-in OE-Core fixes and standalone builds (SF-308)

These results come from local builds. They are not physical, production-provider,
mainnet or public-distribution acceptance, and they do not establish a Layer Index
listing.

### Audit of the integration layer's core-recipe appends

Each append was checked against the pinned revisions and against the OE-Core
heads fetched on 2026-11-24: master `7412d08a`, scarthgap `d521a7d8` and
wrynose `16a03ed6`.

| Append | Series | Upstream status | Resolution |
|---|---|---|---|
| openssl `PARALLEL_MAKEINST = "-j1"` | all | Unfixed: recursive `install_sw` is unchanged in openssl 3.5.9 and 4.1.0. | Moved to the opt-in class. `PARALLEL_MAKEINST[vardepvalue] = "1"`, so it is signature-neutral. |
| libcap install-time cross toolchain | Wrynose (2.77; master 2.78) | Unfixed: `do_install` still omits CC/AR/RANLIB/OBJCOPY/BUILD_CC. | Moved to the opt-in class. Applied on Wrynose only, to all variants. |
| xzmore `${bindir}/bash` plus RDEPENDS | Wrynose (5.8.2; head 5.8.4) | Unfixed: only `xz-ptest` declares bash. | Moved to the opt-in class. Applied to the Wrynose target only. |
| glibc AArch64 usrmerge loader/linker-script guard | all | Not an upstream defect. It is a check-only guard that 0d23a1ec added after a reused local cache produced an incompatible layout. | Kept in the integration layer only. A fresh cache does not need it. |

`meta-suifleet/classes/suifleet-core-fixes.bbclass` is never inherited by the
layer. A consumer opts in with `INHERIT += "suifleet-core-fixes"` in a distro or
local configuration. The integration layer's three bbappends now contain only
`inherit suifleet-core-fixes`. No login, SSH, network, image, machine or
QA-suppression policy was moved into the software layer. The remaining
standalone requirements are OE-Core, meta-oe, the Scarthgap Rust mixin, the
systemd and usrmerge distro features, and the optional codec artifact directory.
These are documented in the layer README.

### Offline tests

`scripts/tests/test_yocto_core_fixes.py` (9 tests) runs from
`make test-yocto-packaging` and checks the following:

- It executes the class's anonymous function against a fake datastore for every
  recipe, series and variant scope.
- The layer never enables the class.
- The class is exported and contains no board, access or QA policy.
- The integration bbappends only inherit it.

The core-fix, export, runtime-packaging and Pi-image-inspection unit tests all
pass, as does `check_yocto_profiles.py`.

### Integration-profile signature equivalence

`bitbake -S none openssl libcap xz openssl-native libcap-native xz-native glibc
systemd suifleet` was run on each pinned series. The configuration used
`MACHINE = "raspberrypi3-64"`, the kas distro settings and
`BB_SIGNATURE_HANDLER = "OEBasicHash"`, with the codec and test-component recipes
masked because their local artifacts are absent. Two layer sets were compared:
both layers at main `212d989a`, and this branch's layers. Each run wrote 172
sigdata files for those recipes, and the file names (that is, the task hashes)
were identical on both Scarthgap and Wrynose. `bitbake-dumpsig` confirmed that
the Wrynose xz `do_install` depends on `suifleet_xz_interpreter`, and that libcap
`EXTRA_OEMAKE` carries the install toolchain. The existing kas profiles therefore
keep their exact task signatures and sstate.

### Exported layer and yocto-check-layer

Two exports of commit `efd1e58c` produced identical 23-member archives, SHA-256
`67a01d7bcfe15bf4327482ea91facbf94179e42bf429030b228f9fc0044194dc`. The class is
included. The extracted export was checked without the integration layer, using
umask 022, `MACHINE = "qemuarm64"`, the systemd/usrmerge distro settings, the
existing downloads, and the codec artifacts described below:

```
yocto-check-layer <export>/meta-suifleet --dependency <pinned meta-oe> --no-auto-dependency
```

- **Scarthgap** (poky `a53cae3d`, meta-oe `ae7dfb12`, meta-lts-mixins `eedcdc74`):
  exit 0 and `PASS`. 8 tests ran in 404 s, with 2 BSP/Distro skips. The 1
  unexpected success is the upstream expectedFailure-decorated patch-status
  test; the layer has no patches.
- **Wrynose** (OE-Core `0f03c732`, bitbake `51fd94a6`, meta-oe `f28a144a`):
  exit 0 and `PASS`. 9 tests ran in 550 s, including `test_security`, with 2
  BSP/Distro skips.

Both runs passed the parse, README, environment, signatures, world and
world_inherit_class checks. Because the class is not inherited, the unchanged
baseline signatures confirm that the layer alone alters no other recipe.

### Codec artifacts

Without the five artifacts, every parse of the main-branch codec recipe fails;
SF-307 tracks parse safety. The artifacts were rebuilt from the pinned source:
a clean `--shared` clone at `b9bb2ef6210624d732ab2de2075ea1826e5300c1`, with
`pnpm install --frozen-lockfile` and `scripts/package_walrus_codec.py`, run in
`suifleet-dev:latest` (Node 22.22.2, pnpm 11.0.8). `--recipe` verification
matched all five recipe SHA-256 pins: `4bdf599f…`, `649f85e5…`, `91811ce2…`,
`eb308bd5…` and `dcd292a9…`.

### Standalone recipe and image builds: not yet completed

The planned acceptance build uses a fresh build directory and the extracted
export above, without the integration layer and with `INHERIT +=
"suifleet-core-fixes"`. It targets `MACHINE = "qemuarm64"` with
`bitbake -k suifleet suifleet-node suifleet-walrus-codec core-image-minimal` on
both series (`xz libcap` are added on Wrynose to force their package QA). The
build uses read-only sstate mirrors and copies of the existing hash-equivalence
databases.

So far that build has only been planned, not run. The shared host's root
filesystem stayed between 1.4 and 16 GB free because of concurrent builds, and
the build needs about 1,260 tasks on Scarthgap and about 4,740 on Wrynose. Every
earlier build carried the integration glibc guard, so target sstate keyed on
stock glibc does not exist. `bitbake -n` plans were:

- **Scarthgap image:** 5016 tasks, 1260 to execute. Sstate matched 1899 of 2277.
- **Wrynose recipes:** 4738 tasks. Sstate matched 1417 of 2413, and 996 sstate
  tasks were missed, including glibc, gcc, libstd-rs and linux-yocto.

The Wrynose attempt began at 14:52 with 15 GB free. BitBake attempted 1160
tasks: 1057 were reused, and the rest executed without any failures, among them `libcap-native` `do_compile`/`do_install` with the class's
toolchain arguments. BitBake's disk monitor (`STOPTASKS` at 5 GB, set to protect
the other builds) then stopped the attempt; no target task had executed. Scratch
space was released afterwards.

This means no standalone recipe or image task execution, package QA or emulated
boot has yet been established for the extracted export. Rerun the build when
roughly 40 GB or more is free, and record the results here. Until then, the
evidence consists of the parse and yocto-check-layer results above, together
with the integration-profile builds whose openssl, libcap and xz task signatures
the class reproduces exactly.


## Limits and publication follow-through

No new bootable image, emulated boot, physical programming, public-testnet
transaction, mainnet transaction, production credential or manual wallet test is
claimed. Existing physical/real-wallet/provider acceptance stays in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
The metadata split preserves existing recipe bodies and Pi kas selection;
historical image/boot receipts retain their original revisions and scope.

No standalone repository, independent public source/codec artifacts, release
branches or Layer Index listing were published. The release operator must finish
[the source and submission checklist](../suifleet-edge/meta-suifleet/RELEASE.md)
with explicit authorization, anonymous downloads and verified remote IDs before
advertising those distribution channels. Existing core Git source access is
public; the independent distribution publication step remains outstanding.
Hosted exact-head/base CI, normal automatic testnet gates and independent review
are pending coordinator acceptance, not claimed by these local results.
