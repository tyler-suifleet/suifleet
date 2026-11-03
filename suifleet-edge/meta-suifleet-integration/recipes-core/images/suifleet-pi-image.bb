SUMMARY = "SuiFleet wired Raspberry Pi development SD image"
LICENSE = "MIT"
IMAGE_INSTALL = "packagegroup-core-boot ${CORE_IMAGE_EXTRA_INSTALL}"
IMAGE_LINGUAS = " "
inherit core-image
COMPATIBLE_MACHINE = "^raspberrypi3-64$"
IMAGE_FEATURES += "ssh-server-openssh"
IMAGE_INSTALL:append = " kernel-modules udev-rules-rpi suifleet suifleet-walrus-codec suifleet-test-component suifleet-pi-access ca-certificates systemd iproute2 ethtool sudo"
IMAGE_FSTYPES = "wic.bz2 wic.bmap ext4"
# Writable state is deliberately retained on the root filesystem.
IMAGE_ROOTFS_EXTRA_SPACE = "524288"

# Refuse an inaccessible development image. Keys belong in a private local layer.
ROOTFS_POSTPROCESS_COMMAND += "suifleet_require_operator_key; "
python suifleet_require_operator_key() {
    import os
    path = os.path.join(d.getVar('IMAGE_ROOTFS'), 'home/operator/.ssh/authorized_keys')
    if not os.path.isfile(path):
        bb.fatal('Install operator authorized_keys through a private local layer; see docs/PI_IMAGE.md')
    keys = open(path).read().splitlines()
    if not keys or any(not line.startswith(('ssh-ed25519 ', 'ssh-rsa ', 'ecdsa-')) for line in keys):
        bb.fatal('operator authorized_keys must contain only SSH public keys')
}
