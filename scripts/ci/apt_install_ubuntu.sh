#!/usr/bin/env bash
# Install Ubuntu archive packages on a hosted CI runner without reading the
# runner image's unrelated third-party APT repositories. A transient 403 from
# packages.microsoft.com failed `apt-get update` in CI run 46052058863 before
# any test ran; these jobs need only Ubuntu packages.
#
# Usage: scripts/ci/apt_install_ubuntu.sh PACKAGE...
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: $0 PACKAGE..." >&2
  exit 2
fi

parts=$(mktemp -d)
trap 'rm -rf "$parts"' EXIT
# Ubuntu 24.04 images define the archive in deb822 ubuntu.sources; 22.04
# images use /etc/apt/sources.list, which this option leaves in effect.
if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
  cp /etc/apt/sources.list.d/ubuntu.sources "$parts/"
fi
chmod 0755 "$parts"

apt_opts=(-o "Dir::Etc::SourceParts=$parts")
sudo apt-get "${apt_opts[@]}" update
sudo apt-get "${apt_opts[@]}" install --yes --no-install-recommends "$@"
