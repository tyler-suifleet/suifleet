# SuiFleet first-boot enrollment

Add this layer to `BBLAYERS` alongside the public `meta-suifleet` layer at
`c78e6297691b38da87989a7ec279734a6669e379`. On a systemd distro, build and
install the `suifleet` package normally. This layer backports the first-boot
enrollment command and ships its systemd unit. It supports the same Yocto
series as the pinned software layer and can be used with a custom BSP.

Prepare the public batch settings and per-device provisioning media at runtime.
See the [customer Yocto guide](../../docs/CUSTOMER_YOCTO.md) and
[enrollment guide](../../docs/ENROLLMENT.md#automatic-linux-first-boot).

The companion also builds the migration-capable package revision `1.0-r1`.
BitBake resolves its guarded service units and provision script ahead of
the files in the public layer. The migration helper runs at online package
upgrade or by operator command, and image construction skips the upgrade hook.
See the [migration procedure](../../docs/INSTALL_DEVICE_CLIENT.md#migrate-an-enrolled-linux-device-in-place).
