FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

SRC_URI:append = " file://first-boot-enrollment.patch file://suifleet-first-boot-enrollment.service"
SYSTEMD_SERVICE:${PN}:append = " suifleet-first-boot-enrollment.service"

do_install:append() {
    install -m 0644 ${SUIFLEET_UNPACKDIR}/suifleet-first-boot-enrollment.service \
        ${D}${systemd_system_unitdir}/
}
