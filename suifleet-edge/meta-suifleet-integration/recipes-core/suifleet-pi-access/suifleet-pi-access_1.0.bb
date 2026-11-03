SUMMARY = "Wired DHCP and authenticated development access for the Pi image"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"
SRC_URI = "file://20-wired.network file://60-suifleet.conf file://operator-sudo"
# Preserve core unpack layout on both supported LTS profiles.
SUIFLEET_UNPACKDIR = "${@d.getVar('UNPACKDIR') or d.getVar('WORKDIR')}"
S = "${SUIFLEET_UNPACKDIR}"
inherit allarch useradd
USERADD_PACKAGES = "${PN}"
USERADD_PARAM:${PN} = "--create-home --gid operator --no-user-group --shell /bin/sh --password '*' operator"
RDEPENDS:${PN} = "openssh-sshd sudo systemd"
do_install() {
    install -d -m 0750 -o operator -g operator ${D}/home/operator
    install -D -m 0644 ${SUIFLEET_UNPACKDIR}/20-wired.network ${D}${sysconfdir}/systemd/network/20-wired.network
    install -D -m 0644 ${SUIFLEET_UNPACKDIR}/60-suifleet.conf ${D}${sysconfdir}/ssh/sshd_config.d/60-suifleet.conf
    install -d ${D}${sysconfdir}/systemd/system/multi-user.target.wants ${D}${sysconfdir}/systemd/system/sysinit.target.wants
    ln -s ${systemd_system_unitdir}/systemd-networkd.service ${D}${sysconfdir}/systemd/system/multi-user.target.wants/systemd-networkd.service
    ln -s ${systemd_system_unitdir}/systemd-resolved.service ${D}${sysconfdir}/systemd/system/multi-user.target.wants/systemd-resolved.service
    ln -s ${systemd_system_unitdir}/systemd-timesyncd.service ${D}${sysconfdir}/systemd/system/sysinit.target.wants/systemd-timesyncd.service
    install -d -m 0750 ${D}${sysconfdir}/sudoers.d
    install -D -m 0440 ${SUIFLEET_UNPACKDIR}/operator-sudo ${D}${sysconfdir}/sudoers.d/operator
}
FILES:${PN} += "${sysconfdir} /home/operator"
