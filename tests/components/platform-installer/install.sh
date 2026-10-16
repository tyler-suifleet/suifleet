#!/bin/sh
# Linux installer-script integration fixture. No OS installation or reboot.
# SuiFleet appends only the already digest-verified artifact; the operator
# explicitly chooses this script and its destination in apply.command.
set -eu
[ "$#" -eq 3 ] || exit 64
destination=$1
receipt_logger=$2
artifact=$3
/usr/bin/install -m 0644 -- "$artifact" "$destination"
# This explicit completion receipt says the command completed, never booted.
exec /usr/bin/python3 "$receipt_logger" "$artifact"
