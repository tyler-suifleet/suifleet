SUMMARY = "Node.js runtime for the SuiFleet Walrus archive codec"
DESCRIPTION = "The official Node.js linux-arm64 release binary, privately installed \
for the keyless SF-082 codec. Only the node executable and its license are shipped: \
no npm, headers or global modules."
HOMEPAGE = "https://nodejs.org/"
# Same declaration as meta-oe's nodejs recipe; the full LICENSE (including the
# bundled V8, ICU, OpenSSL, libuv and other notices) is installed alongside.
LICENSE = "MIT & ISC & BSD-2-Clause & BSD-3-Clause & Artistic-2.0 & Apache-2.0"
LIC_FILES_CHKSUM = "file://LICENSE;md5=b195f4ea4368177a2fd84b879f09cba8"

# The codec manifest (scripts/build_walrus_codec.cjs) targets exactly this
# release. SHA-256 from https://nodejs.org/dist/v22.22.2/SHASUMS256.txt, whose
# detached signature verified against Node.js releaser key
# 890C08DB8579162FEE0DF9DB8BEAB4DFCF555EF4 (docs/YOCTO_PACKAGING.md).
SRC_URI = "https://nodejs.org/dist/v${PV}/node-v${PV}-linux-arm64.tar.xz;sha256sum=e9e1930fd321a470e29bb68f30318bf58e3ecb4acb4f1533fb19c58328a091fe"
SUIFLEET_UNPACKDIR = "${@d.getVar('UNPACKDIR') or d.getVar('WORKDIR')}"
S = "${SUIFLEET_UNPACKDIR}/node-v${PV}-linux-arm64"

# Prebuilt AArch64 glibc binary (needs glibc >= 2.28, libstdc++ GLIBCXX_3.4.21);
# runtime libraries come from the image's shared-library dependencies.
COMPATIBLE_HOST = "aarch64.*-linux"
SUIFLEET_NODE_DIR = "${libdir}/suifleet/node"

do_configure[noexec] = "1"
do_compile[noexec] = "1"

do_install() {
    install -D -m 0755 ${S}/bin/node ${D}${SUIFLEET_NODE_DIR}/bin/node
    install -m 0644 ${S}/LICENSE ${D}${SUIFLEET_NODE_DIR}/LICENSE
}

# The official binary is shipped byte-for-byte; do not re-strip or split it.
INHIBIT_PACKAGE_STRIP = "1"
INHIBIT_PACKAGE_DEBUG_SPLIT = "1"
INHIBIT_SYSROOT_STRIP = "1"
INSANE_SKIP:${PN} += "already-stripped ldflags"

FILES:${PN} = "${SUIFLEET_NODE_DIR}"
