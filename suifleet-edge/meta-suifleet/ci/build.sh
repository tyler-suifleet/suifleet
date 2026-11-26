#!/usr/bin/env bash
set -eo pipefail
umask 022
layer=$(cd "$(dirname "$0")/.." && pwd)
series=${1:?usage: build.sh SERIES NEW_WORK_DIRECTORY [check|build|all]}
work=${2:?provide a new work directory}
mode=${3:-all}
case "$mode" in check|build|all) ;; *) exit 2 ;; esac
mkdir -- "$work"
work=$(cd "$work" && pwd)
export GIT_TERMINAL_PROMPT=0
python3 - "$layer" "$series" > "$work/pins" <<'PY'
import json
import pathlib
import re
import sys

layer, series = pathlib.Path(sys.argv[1]), sys.argv[2]
pins = json.loads((layer / "ci/pins.json").read_text())
conf = (layer / "conf/layer.conf").read_text()
declared = re.search(r'^LAYERSERIES_COMPAT_meta-suifleet = "([^"]+)"$', conf, re.M)
if not declared or set(declared[1].split()) != pins.keys():
    sys.exit("CI pins and LAYERSERIES_COMPAT disagree")
for repo, (url, commit) in pins[series].items():
    if not re.fullmatch(r"[a-z-]+", repo) or not url.startswith("https://") or not re.fullmatch(r"[a-f0-9]{40}", commit):
        sys.exit("Invalid public dependency pin")
    print(repo, url, commit)
PY
while read -r repo url commit; do
    dest="$work/$repo"
    if [[ "$repo" == bitbake ]]; then dest="$work/openembedded-core/bitbake"; fi
    git init -q "$dest"
    if [[ -n "${SUIFLEET_OE_REFERENCE:-}" ]]; then
        url="$SUIFLEET_OE_REFERENCE/$repo"
    fi
    git -C "$dest" fetch -q --depth=1 "$url" "$commit"
    git -C "$dest" checkout -q --detach FETCH_HEAD
    [[ $(git -C "$dest" rev-parse HEAD) == "$commit" ]]
    printf '%s %s\n' "$repo" "$commit"
done < "$work/pins"

core="$work/poky"
extra="$core/meta-poky $work/meta-lts-mixins"
distro=poky
if [[ "$series" == wrynose ]]; then
    core="$work/openembedded-core"
    extra=
    distro=nodistro
fi
source "$core/oe-init-build-env" "$work/build"
bitbake --version
meta_oe="$work/meta-openembedded/meta-oe"
cat > conf/bblayers.conf <<LAYERS
BBPATH = "\${TOPDIR}"
BBLAYERS = "$core/meta $extra $meta_oe"
LAYERS
cat > conf/local.conf <<CONF
CONF_VERSION = "2"
MACHINE = "qemuarm64"
DISTRO = "$distro"
BB_NUMBER_THREADS = "2"
BB_NUMBER_PARSE_THREADS = "2"
PARALLEL_MAKE = "-j4"
RUST_LLVM_TARGETS = "AArch64;X86"
PACKAGE_CLASSES = "package_ipk"
DISTRO_FEATURES:append = " systemd usrmerge"
CONF
if [[ "$series" == wrynose ]]; then
    printf 'INIT_MANAGER = "systemd"\n' >> conf/local.conf
else
    cat >> conf/local.conf <<'CONF'
DISTRO_FEATURES_BACKFILL_CONSIDERED:append = " sysvinit"
VIRTUAL-RUNTIME_init_manager = "systemd"
VIRTUAL-RUNTIME_initscripts = ""
CONF
fi
# Check default upstream signatures before opting into the core fixes.
if [[ "$mode" != build ]]; then
    yocto-check-layer "$layer" --dependency "$meta_oe" --no-auto-dependency
fi
if [[ "$mode" == check ]]; then exit 0; fi
printf 'BBLAYERS += "%s"\n' "$layer" >> conf/bblayers.conf
cat >> conf/local.conf <<'CONF'
INHERIT += "suifleet-core-fixes create-spdx rm_work"
RM_WORK_EXCLUDE += "suifleet suifleet-node suifleet-walrus-codec"
BB_STRICT_CHECKSUM = "1"
CONF
if [[ -n "${SUIFLEET_OE_SSTATE_MIRRORS:-}" ]]; then
    printf 'SSTATE_MIRRORS = "%s"\n' "$SUIFLEET_OE_SSTATE_MIRRORS" >> conf/local.conf
fi

recipes=(suifleet suifleet-node suifleet-walrus-codec)
# A fresh DL_DIR is intentional. The workflow supplies no credentials.
bitbake -c fetch "${recipes[@]}"
bitbake -c verify_lock suifleet
bitbake -c verify_codec suifleet-walrus-codec
bitbake -c populate_lic "${recipes[@]}"
bitbake -c package_qa "${recipes[@]}"
bitbake -c package_write_ipk "${recipes[@]}"
if [[ "$series" == wrynose ]]; then
    bitbake -c create_package_spdx "${recipes[@]}"
else
    bitbake -c create_runtime_spdx "${recipes[@]}"
fi
python3 "$layer/ci/packages.py" "$PWD"
