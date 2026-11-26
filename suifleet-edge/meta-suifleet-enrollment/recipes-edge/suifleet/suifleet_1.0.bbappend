FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

SRC_URI:append = " file://first-boot-enrollment.patch file://suifleet-first-boot-enrollment.service file://suifleet-migrate"
SYSTEMD_SERVICE:${PN}:append = " suifleet-first-boot-enrollment.service"

PR = "r1"
RDEPENDS:${PN}:append = " util-linux-flock"

do_install:append() {
    install -m 0644 ${SUIFLEET_UNPACKDIR}/suifleet-first-boot-enrollment.service \
        ${D}${systemd_system_unitdir}/
    install -D -m 0755 ${SUIFLEET_UNPACKDIR}/suifleet-migrate ${D}${sbindir}/suifleet-migrate
}

# Park legacy clients before systemd reloads/restarts units on an online upgrade.
pkg_postinst:${PN}() {
    if [ -z "$D" ]; then
        ${sbindir}/suifleet-migrate package-upgrade || exit 1
    fi
}
