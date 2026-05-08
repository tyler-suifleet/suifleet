SUMMARY = "SuiFleet decentralized edge deployment client"
HOMEPAGE = "https://github.com/tyler-suifleet/suifleet"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=11afb1fbbd23890e736d2e4025d3e2c9"

SRC_URI = " \
    git://github.com/tyler-suifleet/suifleet.git;protocol=https;branch=main \
    file://suifleet.service \
    file://config.toml.example \
"
SRCREV = "${AUTOREV}"
S = "${WORKDIR}/git"

inherit cargo systemd pkgconfig

DEPENDS += "openssl systemd"
RDEPENDS:${PN} += "ca-certificates"

CARGO_SRC_DIR = "suifleet-edge"

EXTRA_OECARGO_FLAGS = "--features systemd"

SYSTEMD_SERVICE:${PN} = "suifleet.service"
SYSTEMD_AUTO_ENABLE:${PN} = "enable"

do_install:append() {
    install -d ${D}${sysconfdir}/suifleet
    install -m 0600 ${WORKDIR}/config.toml.example ${D}${sysconfdir}/suifleet/config.toml.example
}

FILES:${PN} += "${sysconfdir}/suifleet"
