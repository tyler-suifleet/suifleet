# Opt-in OE-Core recipe fixes for building meta-suifleet on its pinned series.
#
# This class is never inherited automatically: adding meta-suifleet to BBLAYERS
# changes no other recipe. A standalone distro or local.conf that hits the
# defects below may enable it explicitly with:
#
#     INHERIT += "suifleet-core-fixes"
#
# It acts only on the recipes listed here. It contains no login, SSH, network,
# image, machine or hardware policy and suppresses no QA check. Each fix was
# observed on the pinned revisions in the core repository's kas profiles and
# was still unfixed on OE-Core scarthgap, wrynose and master on 2026-11-24.
# Re-check and drop a fix once upstream carries an equivalent change.
#
# - openssl (all series and variants): the recursive install_sw target can
#   relink libcrypto while modules/programs link against it. Serialize only
#   installation; compilation stays parallel. PARALLEL_MAKEINST is
#   signature-neutral (vardepvalue). https://github.com/openssl/openssl/issues/27074
# - libcap (Wrynose 2.77): the install target can relink capsh, but the recipe
#   passes the cross toolchain only to do_compile, so the relink falls back to
#   the host gcc with target flags.
# - xz (Wrynose 5.8.x, target only): the generated xzmore selects /bin/bash but
#   only xz-ptest declares bash. Use the usrmerge path that bash advertises and
#   declare the runtime dependency.

python __anonymous() {
    series = (d.getVar('LAYERSERIES_CORENAMES') or '').split()
    bpn = d.getVar('BPN')
    if bpn == 'openssl':
        d.setVar('PARALLEL_MAKEINST', '-j1')
    elif bpn == 'libcap' and 'wrynose' in series:
        d.appendVar('EXTRA_OEMAKE', ' CC="${CC}" AR="${AR}" RANLIB="${RANLIB}" OBJCOPY="${OBJCOPY}" BUILD_CC="${BUILD_CC}" BUILD_LDFLAGS="${BUILD_LDFLAGS}"')
    elif bpn == 'xz' and 'wrynose' in series and d.getVar('CLASSOVERRIDE') == 'class-target':
        d.appendVar('RDEPENDS:' + d.getVar('PN'), ' bash')
        d.appendVar('do_install', '\n    suifleet_xz_interpreter\n')
}

suifleet_xz_interpreter() {
    sed -i '1s|^#!/bin/bash$|#!${bindir}/bash|' ${D}${bindir}/xzmore
}
