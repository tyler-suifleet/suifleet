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

# Generated with the pinned daemon source and frozen pnpm lockfile.
SRC_URI = "https://raw.githubusercontent.com/tyler-suifleet/meta-suifleet/a4d5a3b16c7e3ab9fdccfd13032b7b3af4bfb87a/suifleet-walrus-codec-${SUIFLEET_CODEC_SOURCE_REV}.tar.gz;subdir=codec"
SRC_URI[sha256sum] = "208d4b8fd1d38fbafe4a6f49c08645ffb6b6e6c0052dbe266a4398f7928b96a6"
SUIFLEET_UNPACKDIR = "${@d.getVar('UNPACKDIR') or d.getVar('WORKDIR')}"
S = "${SUIFLEET_UNPACKDIR}/codec"

SUIFLEET_CODEC_SOURCE_REV = "212d989af09f847e7f60941a6814b7e6715e997b"
SUIFLEET_CODEC_SHA256[walrus-device-codec.cjs] = "4bdf599f13185393ca20d89a8c8ba6330e96fe596fa4028d91ae9de76972d423"
SUIFLEET_CODEC_SHA256[walrus_wasm_bg.wasm] = "649f85e5306ebb14e5d36b1edfdb03be5deef4b5e21c9ed06ca030b03b41335a"
SUIFLEET_CODEC_SHA256[manifest.json] = "91811ce2611bdee2e6b7eac15ddd185178971d8b5ba4e4d7d4ca2c7bddc6cb04"
SUIFLEET_CODEC_SHA256[THIRD_PARTY_LICENSES.txt] = "eb308bd5d1012b4e741d853817657298eaa9cc4aa7d713d7b8a71a3872085cbc"
SUIFLEET_CODEC_SHA256[provenance.json] = "31b27fa502ef3e192855e9db2fb4a819c851615dc7c231ee5e911d865dd0792b"
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
