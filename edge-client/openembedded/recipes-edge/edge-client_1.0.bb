SUMMARY = "SUI decentralized edge deployment client"
HOMEPAGE = ""
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=placeholder"

SRC_URI = " \
    git://github.com/placeholder/sui-edge-client.git;protocol=https;branch=main \
    file://edge-client.service \
    file://config.toml.example \
"
SRCREV = "${AUTOREV}"
S = "${WORKDIR}/git"

inherit cargo systemd pkgconfig

DEPENDS += "openssl"
RDEPENDS:${PN} += "swupdate ca-certificates"

CARGO_SRC_DIR = "edge-client"

EXTRA_OECARGO_FLAGS = "--features systemd"

SYSTEMD_SERVICE:${PN} = "edge-client.service"
SYSTEMD_AUTO_ENABLE:${PN} = "enable"

do_install:append() {
    install -d ${D}${sysconfdir}/edge-client
    install -m 0600 ${WORKDIR}/config.toml.example ${D}${sysconfdir}/edge-client/config.toml.example
}

FILES:${PN} += "${sysconfdir}/edge-client"
