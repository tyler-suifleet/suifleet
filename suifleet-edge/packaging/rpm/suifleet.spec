Name: suifleet
Version: %{fleet_version}
Release: %{fleet_release}
Summary: SuiFleet edge client and isolated device signer
License: MIT
URL: https://github.com/tyler-suifleet/suifleet
Source0: payload.tar
ExclusiveArch: x86_64
Requires: systemd, ca-certificates, coreutils, grep
Requires(pre): shadow-utils, systemd, coreutils, glibc-common
Requires(preun): systemd
Requires(post): systemd
Requires(postun): systemd
%global debug_package %{nil}
# The release builder produced the checksummed executable and SBOM.
# Keep executable bytes identical to the release manifest's provenance.
%global __os_install_post %{nil}
%global use_source_date_epoch_as_buildtime 1
%global clamp_mtime_to_source_date_epoch 1
%global _buildhost suifleet-rpm

%description
Authenticated component delivery with a separate policy-limited signing
service. Provisioning and service activation are explicit operator steps.

%install
mkdir -p "%{buildroot}"
tar -xf "%{SOURCE0}" -C "%{buildroot}"

%pre
set -eu
for account in suifleet suifleet-signer; do
    if ! getent group "$account" >/dev/null; then
        groupadd --system "$account"
    fi
    if ! getent passwd "$account" >/dev/null; then
        useradd --system --gid "$account" --no-create-home --shell /sbin/nologin "$account"
    fi
done
daemon_uid=$(id -u suifleet)
signer_uid=$(id -u suifleet-signer)
daemon_gid=$(getent group suifleet | cut -d: -f3)
signer_gid=$(getent group suifleet-signer | cut -d: -f3)
if [ "$daemon_uid" = 0 ] || [ "$signer_uid" = 0 ] || [ "$daemon_uid" = "$signer_uid" ] ||
   [ "$daemon_gid" = 0 ] || [ "$signer_gid" = 0 ] || [ "$daemon_gid" = "$signer_gid" ] ||
   [ "$(id -g suifleet)" != "$daemon_gid" ] || [ "$(id -g suifleet-signer)" != "$signer_gid" ]; then
    echo 'SuiFleet requires separate unprivileged daemon and signer users and groups' >&2
    exit 1
fi
for account in suifleet suifleet-signer; do
    case " $(id -G "$account") " in
        *" 0 "*) echo "$account must not belong to root group" >&2; exit 1 ;;
    esac
done
case " $(id -G suifleet) " in
    *" $signer_gid "*) echo 'Daemon must not belong to signer group' >&2; exit 1 ;;
esac
for path in /etc/suifleet /var/lib/suifleet; do
    if [ -L "$path" ] || { [ -e "$path" ] && [ ! -d "$path" ]; }; then
        echo "Refusing unsafe SuiFleet directory $path" >&2
        exit 1
    fi
done
if [ "$1" -gt 1 ] && [ -d /run/systemd/system ]; then
    systemctl stop suifleet-enrollment.path suifleet-signer.socket
    systemctl stop suifleet.service suifleet-signer.service suifleet-provision.service
fi
install -d -m 0750 -o root -g suifleet /etc/suifleet
install -d -m 0700 -o suifleet -g suifleet /var/lib/suifleet

%post
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
fi

%preun
set -eu
if [ "$1" -eq 0 ]; then
    if [ -d /run/systemd/system ]; then
        systemctl stop suifleet-enrollment.path suifleet-signer.socket
        systemctl stop suifleet.service suifleet-signer.service suifleet-provision.service
    fi
    systemctl --no-reload disable suifleet.service suifleet-enrollment.path suifleet-signer.socket suifleet-signer.service suifleet-provision.service
fi

%postun
if [ "$1" -eq 0 ] && [ -d /run/systemd/system ]; then
    systemctl daemon-reload
fi

%files
%license /usr/share/licenses/suifleet/LICENSE
%doc /usr/share/doc/suifleet
%config(noreplace) /etc/suifleet/release-trust.toml
/usr/bin/suifleet
/usr/libexec/suifleet-provision
/usr/lib/systemd/system/suifleet*
