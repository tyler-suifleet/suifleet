SUMMARY = "Harmless SuiFleet OTA test updater (test images only)"
DESCRIPTION = "suifleet-test-updater applies the generated SF-030 test component \
under /var/lib/suifleet/test-components and records a version/run-ID receipt. \
Configure it as apply.command on test devices only; it never touches boot \
partitions, firmware or services. Do not install it in production images."
HOMEPAGE = "https://github.com/tyler-suifleet/suifleet"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

# The layer lives inside the SuiFleet repository; package the same updater the
# repository's fixture tests exercise instead of a copy.
FILESEXTRAPATHS:prepend := "${THISDIR}/../../../../tests/components/suifleet-test-app:"
SRC_URI = "file://suifleet-test-updater"
# Preserve core's unpack root; Scarthgap predates UNPACKDIR.
SUIFLEET_UNPACKDIR = "${@d.getVar('UNPACKDIR') or d.getVar('WORKDIR')}"
S = "${SUIFLEET_UNPACKDIR}"

inherit allarch

do_configure[noexec] = "1"
do_compile[noexec] = "1"

do_install() {
    install -D -m 0755 ${SUIFLEET_UNPACKDIR}/suifleet-test-updater ${D}${bindir}/suifleet-test-updater
}

# POSIX sh plus sed, wc, sha256sum, cut, cp, mv, date and uname (busybox).
RDEPENDS:${PN} = "${VIRTUAL-RUNTIME_base-utils}"
