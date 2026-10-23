SUMMARY = "SuiFleet keyless Walrus archive codec"
DESCRIPTION = "The SF-082 esbuild codec bundle, its Walrus WASM module and manifest, \
run by the daemon with the pinned Node runtime for encrypted fleet archives. It \
receives ciphertext and public IDs only, never keys or signing access."
HOMEPAGE = "https://github.com/tyler-suifleet/suifleet"
# The elected licenses of every bundled npm package and every Rust crate in
# the WASM module (THIRD_PARTY_LICENSES.txt; provenance.json "licenses").
# @mysten/walrus-wasm declares Apache-2.0 in its SPDX headers, not package.json;
# its crates come from the reviewed scripts/walrus_wasm_licenses inventory.
# Unicode-3.0 is a common license of the required Rust mixin layer.
LICENSE = "Apache-2.0 & BSD-3-Clause & CC0-1.0 & MIT & Unicode-3.0"
LIC_FILES_CHKSUM = "file://THIRD_PARTY_LICENSES.txt;md5=58ddbd6c47a43049d41c1e1c60972c43"

# Produced outside BitBake from the daemon source at SUIFLEET_CODEC_SOURCE_REV
# with its frozen pnpm lockfile by scripts/package_walrus_codec.py; see
# docs/YOCTO_PACKAGING.md. Every file is pinned below and verified before
# install, so a missing, stale or modified artifact stops the build.
SUIFLEET_WALRUS_CODEC_DIR ??= "${TOPDIR}/suifleet-walrus-codec/${SUIFLEET_CODEC_SOURCE_REV}"
FILESEXTRAPATHS:prepend := "${SUIFLEET_WALRUS_CODEC_DIR}:"
SRC_URI = " \
    file://walrus-device-codec.cjs;subdir=codec \
    file://walrus_wasm_bg.wasm;subdir=codec \
    file://manifest.json;subdir=codec \
    file://THIRD_PARTY_LICENSES.txt;subdir=codec \
    file://provenance.json;subdir=codec \
"
SUIFLEET_UNPACKDIR = "${@d.getVar('UNPACKDIR') or d.getVar('WORKDIR')}"
S = "${SUIFLEET_UNPACKDIR}/codec"

SUIFLEET_CODEC_SOURCE_REV = "2d231cadf3c0c8e58b48dc9f48dd7890faaafa1f"
SUIFLEET_CODEC_SHA256[walrus-device-codec.cjs] = "44cdaf7e616523f593ea05f1a64447ef37d1893643108221c8913481f1d83ec4"
SUIFLEET_CODEC_SHA256[walrus_wasm_bg.wasm] = "649f85e5306ebb14e5d36b1edfdb03be5deef4b5e21c9ed06ca030b03b41335a"
SUIFLEET_CODEC_SHA256[manifest.json] = "642b3cfbc2e20b83dc166fb00494956fc477b83cd7d89ea51e2b25052189010f"
SUIFLEET_CODEC_SHA256[THIRD_PARTY_LICENSES.txt] = "eb308bd5d1012b4e741d853817657298eaa9cc4aa7d713d7b8a71a3872085cbc"
SUIFLEET_CODEC_SHA256[provenance.json] = "e9d248fc32a71956f085b7606fc2efae9cc24ac8fa02c8cf885a97421f08ff6e"
SUIFLEET_CODEC_NODE_VERSION = "22.22.2"
SUIFLEET_CODEC_DIR = "${libdir}/suifleet/walrus-codec"

do_configure[noexec] = "1"
do_compile[noexec] = "1"

python do_verify_codec() {
    import hashlib, json, os
    s = d.getVar('S')
    names = sorted(os.listdir(s))
    pinned = sorted((d.getVarFlags('SUIFLEET_CODEC_SHA256') or {}).keys())
    if names != pinned:
        bb.fatal('codec artifact must contain exactly %s, found %s' % (pinned, names))
    for name in names:
        actual = hashlib.sha256(open(os.path.join(s, name), 'rb').read()).hexdigest()
        if actual != d.getVarFlag('SUIFLEET_CODEC_SHA256', name):
            bb.fatal('codec artifact %s differs from its pin; rebuild with scripts/package_walrus_codec.py' % name)
    manifest = json.load(open(os.path.join(s, 'manifest.json')))
    provenance = json.load(open(os.path.join(s, 'provenance.json')))
    if manifest.get('schema') != 1 or manifest.get('node') != d.getVar('SUIFLEET_CODEC_NODE_VERSION'):
        bb.fatal('codec manifest does not target the packaged Node runtime')
    if provenance.get('source_revision') != d.getVar('SUIFLEET_CODEC_SOURCE_REV'):
        bb.fatal('codec artifact was built from another source revision')
    if ' & '.join(provenance.get('licenses') or []) != d.getVar('LICENSE'):
        bb.fatal('recipe LICENSE differs from the licenses in the codec provenance')
    for name, entry in manifest['files'].items():
        if d.getVarFlag('SUIFLEET_CODEC_SHA256', name) != entry.get('sha256'):
            bb.fatal('codec manifest and recipe disagree on ' + name)
}
addtask verify_codec after do_unpack before do_populate_lic do_configure

do_install() {
    install -d -m 0755 ${D}${SUIFLEET_CODEC_DIR}
    for f in walrus-device-codec.cjs walrus_wasm_bg.wasm manifest.json THIRD_PARTY_LICENSES.txt provenance.json; do
        install -m 0644 ${S}/$f ${D}${SUIFLEET_CODEC_DIR}/$f
    done
}

# The bundle needs exactly this Node release; the WASM loads from the bundle's directory.
RDEPENDS:${PN} = "suifleet-node (= ${SUIFLEET_CODEC_NODE_VERSION})"
FILES:${PN} = "${SUIFLEET_CODEC_DIR}"
