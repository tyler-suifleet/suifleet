SUMMARY = "SuiFleet decentralized edge deployment client"
HOMEPAGE = "https://github.com/tyler-suifleet/suifleet"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=11afb1fbbd23890e736d2e4025d3e2c9"

SRC_URI = " \
    git://github.com/tyler-suifleet/suifleet.git;protocol=https;nobranch=1;destsuffix=git \
    file://suifleet.service \
    file://suifleet-firmware.service \
    file://suifleet-firmware.path \
    file://config.toml.example \
    file://firmware.toml.example \
    file://suifleet-provision.service \
    file://suifleet-provision \
    file://suifleet-enrollment.path \
    file://suifleet-signer.service \
    file://suifleet-signer.socket \
    file://signer.toml.example \
"
SRCREV = "d616fa16e1d3e90eb0f97a669f693dec99546d3c"
# Preserve core's unpack root; Scarthgap predates UNPACKDIR.
SUIFLEET_UNPACKDIR = "${@d.getVar('UNPACKDIR') or d.getVar('WORKDIR')}"
S = "${SUIFLEET_UNPACKDIR}/git"

inherit cargo cargo-update-recipe-crates systemd pkgconfig useradd
require suifleet-crates.inc
SUIFLEET_LOCK_SHA256 = "310102d73d7644815fe05df24917c332136836d64dcff5f7ca35b44c63b2d937"
CARGO_LOCK_SRC_DIR = "${S}/suifleet-edge"
CARGO_BUILD_FLAGS:append = " --bin suifleet --features systemd"
# Every compile uses the dependency graph fetched and verified by BitBake.
export CARGO_NET_OFFLINE = "true"
# Let Yocto split debug symbols and strip the packaged executable itself.
export CARGO_PROFILE_RELEASE_STRIP = "false"
# Native C/assembly in vendored crates lives outside ${S} and ${B}.
CFLAGS:append = " -fdebug-prefix-map=${CARGO_HOME}/bitbake=/usr/src/debug/${PN}/${PV}/vendor -fmacro-prefix-map=${CARGO_HOME}/bitbake=/usr/src/debug/${PN}/${PV}/vendor"

DEPENDS += "systemd"
RDEPENDS:${PN} += "ca-certificates"

CARGO_SRC_DIR = "suifleet-edge"

python do_verify_lock() {
    import hashlib
    lock = open(d.getVar('CARGO_LOCK_PATH'), 'rb').read()
    if hashlib.sha256(lock).hexdigest() != d.getVar('SUIFLEET_LOCK_SHA256'):
        bb.fatal('Cargo.lock differs from the pinned crate graph; regenerate suifleet-crates.inc and its lock checksum')
}
addtask verify_lock after do_patch before do_configure

python do_verify_toolchain() {
    import os
    import re
    import shlex
    import subprocess
    compiler = d.getVar("RUSTC") or os.path.join(d.getVar("STAGING_BINDIR_NATIVE"), "rustc")
    output = subprocess.check_output(shlex.split(compiler) + ["--version"], text=True)
    match = re.match(r"rustc (\d+\.\d+\.\d+)", output)
    if not match or bb.utils.vercmp_string(match.group(1), "1.90.0") < 0:
        bb.fatal("SuiFleet locked dependencies require Rust >= 1.90.0; Scarthgap needs the pinned rust mixin; got " + output)
    bb.note("Verified SuiFleet compiler: " + output.strip())
}
addtask verify_toolchain after do_prepare_recipe_sysroot before do_configure

# Unprivileged daemon user; only suifleet-firmware.service runs as root. The
# device key belongs to the separate signing-service user (SF-053).
USERADD_PACKAGES = "${PN}"
USERADD_PARAM:${PN} = "--system --no-create-home --home-dir /var/lib/suifleet --shell /sbin/nologin --user-group suifleet; \
    --system --no-create-home --home-dir /nonexistent --shell /sbin/nologin --user-group suifleet-signer"

SYSTEMD_SERVICE:${PN} = "suifleet.service suifleet-provision.service suifleet-enrollment.path suifleet-firmware.path suifleet-signer.socket suifleet-signer.service"
SYSTEMD_AUTO_ENABLE:${PN} = "enable"

do_install:append() {
    install -d ${D}${datadir}/suifleet
    printf 'source_revision=%s\nlock_sha256=%s\ncrate_version=%s\n' '${SRCREV}' '${SUIFLEET_LOCK_SHA256}' '0.2.1' > ${D}${datadir}/suifleet/build-info
    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${SUIFLEET_UNPACKDIR}/suifleet.service ${SUIFLEET_UNPACKDIR}/suifleet-firmware.service ${SUIFLEET_UNPACKDIR}/suifleet-firmware.path \
        ${SUIFLEET_UNPACKDIR}/suifleet-provision.service ${SUIFLEET_UNPACKDIR}/suifleet-enrollment.path \
        ${SUIFLEET_UNPACKDIR}/suifleet-signer.service ${SUIFLEET_UNPACKDIR}/suifleet-signer.socket \
        ${D}${systemd_system_unitdir}/
    install -D -m 0755 ${SUIFLEET_UNPACKDIR}/suifleet-provision ${D}${libexecdir}/suifleet-provision
    # /etc/suifleet is read-only to the daemon; config.toml must be root:suifleet 0640.
    install -d -m 0750 -g suifleet ${D}${sysconfdir}/suifleet
    install -m 0640 -g suifleet ${SUIFLEET_UNPACKDIR}/config.toml.example ${D}${sysconfdir}/suifleet/config.toml.example
    # Root-only: names the only command that ever runs as root.
    install -m 0600 ${SUIFLEET_UNPACKDIR}/firmware.toml.example ${D}${sysconfdir}/suifleet/firmware.toml.example
    # Readable by the signing service only (it names the key or PKCS#11 PIN file).
    install -m 0640 -g suifleet-signer ${SUIFLEET_UNPACKDIR}/signer.toml.example ${D}${sysconfdir}/suifleet/signer.toml.example
}

FILES:${PN} += "${sysconfdir}/suifleet ${systemd_system_unitdir}/suifleet*.service ${systemd_system_unitdir}/suifleet*.path ${systemd_system_unitdir}/suifleet*.socket"
RDEPENDS:${PN} += "${VIRTUAL-RUNTIME_base-utils}"
