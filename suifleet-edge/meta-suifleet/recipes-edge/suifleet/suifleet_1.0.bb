SUMMARY = "SuiFleet decentralized edge deployment client"
HOMEPAGE = "https://github.com/tyler-suifleet/suifleet"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=11afb1fbbd23890e736d2e4025d3e2c9"

SRC_URI = " \
    git://github.com/tyler-suifleet/suifleet.git;protocol=https;branch=main \
    file://suifleet.service \
    file://suifleet-firmware.service \
    file://suifleet-firmware.path \
    file://config.toml.example \
    file://firmware.toml.example \
    file://suifleet-provision.service \
    file://suifleet-provision \
    file://suifleet-enrollment.path \
"
SRCREV = "c10ac76e9aae5b3ea8f92a42d8a68068c9bba71f"
S = "${WORKDIR}/git"

inherit cargo cargo-update-recipe-crates systemd pkgconfig useradd
require suifleet-crates.inc
SUIFLEET_LOCK_SHA256 = "965363c17c3e0106dc82411c6cb7b8fb982f13bc1259ce00720a89383c635974"
CARGO_LOCK_SRC_DIR = "${S}/suifleet-edge"
CARGO_BUILD_FLAGS:append = " --bin suifleet --features systemd"
# Every compile uses the dependency graph fetched and verified by BitBake.
export CARGO_NET_OFFLINE = "true"
# Let Yocto split debug symbols and strip the packaged executable itself.
export CARGO_PROFILE_RELEASE_STRIP = "false"

DEPENDS += "openssl systemd"
RDEPENDS:${PN} += "ca-certificates"

CARGO_SRC_DIR = "suifleet-edge"

python do_verify_lock() {
    import hashlib
    lock = open(d.getVar('CARGO_LOCK_PATH'), 'rb').read()
    if hashlib.sha256(lock).hexdigest() != d.getVar('SUIFLEET_LOCK_SHA256'):
        bb.fatal('Cargo.lock differs from the pinned crate graph; regenerate suifleet-crates.inc and its lock checksum')
}
addtask verify_lock after do_patch before do_configure

# Unprivileged daemon user; only suifleet-firmware.service runs as root.
USERADD_PACKAGES = "${PN}"
USERADD_PARAM:${PN} = "--system --no-create-home --home-dir /var/lib/suifleet --shell /sbin/nologin --user-group suifleet"

SYSTEMD_SERVICE:${PN} = "suifleet.service suifleet-provision.service suifleet-enrollment.path suifleet-firmware.path"
SYSTEMD_AUTO_ENABLE:${PN} = "enable"

do_install:append() {
    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/suifleet.service ${WORKDIR}/suifleet-firmware.service ${WORKDIR}/suifleet-firmware.path \
        ${WORKDIR}/suifleet-provision.service ${WORKDIR}/suifleet-enrollment.path \
        ${D}${systemd_system_unitdir}/
    install -D -m 0755 ${WORKDIR}/suifleet-provision ${D}${libexecdir}/suifleet-provision
    # /etc/suifleet is read-only to the daemon; config.toml must be root:suifleet 0640.
    install -d -m 0750 -g suifleet ${D}${sysconfdir}/suifleet
    install -m 0640 -g suifleet ${WORKDIR}/config.toml.example ${D}${sysconfdir}/suifleet/config.toml.example
    # Root-only: names the only command that ever runs as root.
    install -m 0600 ${WORKDIR}/firmware.toml.example ${D}${sysconfdir}/suifleet/firmware.toml.example
}

FILES:${PN} += "${sysconfdir}/suifleet ${systemd_system_unitdir}/suifleet*.service ${systemd_system_unitdir}/suifleet*.path"
RDEPENDS:${PN} += "${VIRTUAL-RUNTIME_base-utils}"
