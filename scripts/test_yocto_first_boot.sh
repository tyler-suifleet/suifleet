#!/bin/sh
# Host runner: two fresh, network-isolated containers using the built daemon.
set -eu
image=${1:?usage: test_yocto_first_boot.sh DEV_IMAGE}
case "$image" in -*) exit 2 ;; esac
repository=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test -x "$repository/suifleet-edge/target/debug/suifleet"

run() {
    docker run --rm --network none --user root \
        -v "$repository:/workspace:ro" -w /workspace "$image" /bin/sh -eu -c '
        getent group suifleet >/dev/null || groupadd --system suifleet
        getent passwd suifleet >/dev/null || useradd --system --gid suifleet --no-create-home --home-dir /var/lib/suifleet --shell /sbin/nologin suifleet
        install -d -m0750 -g suifleet /etc/suifleet
        install -Dm0755 suifleet-edge/target/debug/suifleet /usr/bin/suifleet
        install -Dm0755 suifleet-edge/meta-suifleet/recipes-edge/suifleet/files/suifleet-provision /usr/libexec/suifleet-provision
        python3 scripts/test_yocto_first_boot.py
        '
}

first=$(run)
second=$(run)
printf '%s\n%s\n' "$first" "$second"
first_address=$(printf '%s\n' "$first" | tail -n1)
second_address=$(printf '%s\n' "$second" | tail -n1)
test "$first_address" != "$second_address"
printf '%s\n' 'PASS: independent fresh containers have distinct device addresses'
