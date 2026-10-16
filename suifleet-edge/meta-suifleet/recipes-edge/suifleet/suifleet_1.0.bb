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
"
SRCREV = "795de747e066ee97af0e9e0e53deda3c6c71e90c"
# Preserve core's unpack root; Scarthgap predates UNPACKDIR.
SUIFLEET_UNPACKDIR = "${@d.getVar('UNPACKDIR') or d.getVar('WORKDIR')}"
S = "${SUIFLEET_UNPACKDIR}/git"

inherit cargo cargo-update-recipe-crates systemd pkgconfig useradd
require suifleet-crates.inc
SUIFLEET_LOCK_SHA256 = "44aa5b2637fc2b828a796a63e9a550d9c0fa652437cf59ffd31e0b264021290e"
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

# Unprivileged daemon user; only suifleet-firmware.service runs as root.
USERADD_PACKAGES = "${PN}"
USERADD_PARAM:${PN} = "--system --no-create-home --home-dir /var/lib/suifleet --shell /sbin/nologin --user-group suifleet"

SYSTEMD_SERVICE:${PN} = "suifleet.service suifleet-provision.service suifleet-enrollment.path suifleet-firmware.path"
SYSTEMD_AUTO_ENABLE:${PN} = "enable"

do_install:append() {
    install -d ${D}${datadir}/suifleet
    printf 'source_revision=%s\nlock_sha256=%s\ncrate_version=%s\n' '${SRCREV}' '${SUIFLEET_LOCK_SHA256}' '0.2.1' > ${D}${datadir}/suifleet/build-info
    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${SUIFLEET_UNPACKDIR}/suifleet.service ${SUIFLEET_UNPACKDIR}/suifleet-firmware.service ${SUIFLEET_UNPACKDIR}/suifleet-firmware.path \
        ${SUIFLEET_UNPACKDIR}/suifleet-provision.service ${SUIFLEET_UNPACKDIR}/suifleet-enrollment.path \
        ${D}${systemd_system_unitdir}/
    install -D -m 0755 ${SUIFLEET_UNPACKDIR}/suifleet-provision ${D}${libexecdir}/suifleet-provision
    # /etc/suifleet is read-only to the daemon; config.toml must be root:suifleet 0640.
    install -d -m 0750 -g suifleet ${D}${sysconfdir}/suifleet
    install -m 0640 -g suifleet ${SUIFLEET_UNPACKDIR}/config.toml.example ${D}${sysconfdir}/suifleet/config.toml.example
    # Root-only: names the only command that ever runs as root.
    install -m 0600 ${SUIFLEET_UNPACKDIR}/firmware.toml.example ${D}${sysconfdir}/suifleet/firmware.toml.example
}

FILES:${PN} += "${sysconfdir}/suifleet ${systemd_system_unitdir}/suifleet*.service ${systemd_system_unitdir}/suifleet*.path"
RDEPENDS:${PN} += "${VIRTUAL-RUNTIME_base-utils}"
