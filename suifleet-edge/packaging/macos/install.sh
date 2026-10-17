#!/bin/sh
# Install the SuiFleet daemon as a macOS LaunchDaemon (SF-075). Run as root:
#
#   sudo packaging/macos/install.sh path/to/suifleet
#
# Creates the hidden _suifleet user/group when missing, installs the binary
# and launchd job, and provisions the device identity on this machine (never
# in a package). It does not write config.toml, enroll, or start the job.
# Re-running keeps an existing key, configuration and state.
set -eu

binary=${1:?usage: install.sh SUIFLEET_BINARY}
here=$(cd "$(dirname "$0")" && pwd)
user=_suifleet
label=org.suifleet.daemon
etc=/etc/suifleet
state=/var/lib/suifleet
logs=/var/log/suifleet
plist=/Library/LaunchDaemons/$label.plist

[ "$(id -u)" = 0 ] || { echo "install.sh: run as root" >&2; exit 1; }
[ "$(uname -s)" = Darwin ] || { echo "install.sh: macOS only" >&2; exit 1; }
umask 022

# A hidden system account: the first free ID in the 200-400 range, shared by
# the user and its primary group.
if ! dscl . -read "/Users/$user" >/dev/null 2>&1; then
    id=200
    while dscl . -list /Users UniqueID | awk '{print $2}' | grep -qx "$id" \
        || dscl . -list /Groups PrimaryGroupID | awk '{print $2}' | grep -qx "$id"; do
        id=$((id + 1))
        [ "$id" -le 400 ] || { echo "install.sh: no free system ID" >&2; exit 1; }
    done
    dscl . -create "/Groups/$user"
    dscl . -create "/Groups/$user" PrimaryGroupID "$id"
    dscl . -create "/Groups/$user" RealName "SuiFleet daemon"
    dscl . -create "/Users/$user"
    dscl . -create "/Users/$user" UniqueID "$id"
    dscl . -create "/Users/$user" PrimaryGroupID "$id"
    dscl . -create "/Users/$user" UserShell /usr/bin/false
    dscl . -create "/Users/$user" NFSHomeDirectory /var/empty
    dscl . -create "/Users/$user" RealName "SuiFleet daemon"
    dscl . -create "/Users/$user" IsHidden 1
fi

install -d -o root -g wheel -m 0755 /usr/local/bin "$etc"
install -o root -g wheel -m 0755 "$binary" /usr/local/bin/suifleet
install -d -o "$user" -g "$user" -m 0700 "$state"
install -d -o "$user" -g "$user" -m 0750 "$logs"

# The device key: generated here, readable by the daemon user only. The daemon
# refuses to start with a key any other user can reach.
(umask 077 && /usr/local/bin/suifleet --provision "$etc/device.key" >/dev/null)
chown "$user:$user" "$etc/device.key"
chmod 0600 "$etc/device.key"
address=$(/usr/local/bin/suifleet --derive-address "$etc/device.key")
printf '%s\n' "$address" | grep -Eq '^0x[0-9a-f]{64}$'
printf '%s\n' "$address" > "$etc/device.address"
chown "root:$user" "$etc/device.address"
chmod 0640 "$etc/device.address"

install -o root -g wheel -m 0644 "$here/$label.plist" "$plist"
plutil -lint "$plist" >/dev/null
printf 'SuiFleet device address: %s\n' "$address"
echo "Write $etc/config.toml (root-owned, 0644), then: launchctl bootstrap system $plist"
