################################################################################
#
# suifleet
#
################################################################################

# Reviewed SF-292 integration revision, including the portable Linux signer.
SUIFLEET_VERSION = 773700486e111e74e8a5471057157ebbfda44814
SUIFLEET_SITE = $(call github,tyler-suifleet,suifleet,$(SUIFLEET_VERSION))
SUIFLEET_SUBDIR = suifleet-edge
SUIFLEET_LICENSE = MIT
SUIFLEET_LICENSE_FILES = LICENSE
SUIFLEET_CARGO_BUILD_OPTS = --bin suifleet --no-default-features
SUIFLEET_DEPENDENCIES = python3 ca-certificates

define SUIFLEET_USERS
	suifleet -1 suifleet -1 * - /sbin/nologin - SuiFleet daemon
	suifleet-signer -1 suifleet-signer -1 * - /sbin/nologin - SuiFleet signer
endef

define SUIFLEET_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/suifleet-edge/target/$(RUSTC_TARGET_NAME)/$(if $(BR2_ENABLE_DEBUG),debug,release)/suifleet $(TARGET_DIR)/usr/bin/suifleet
	$(INSTALL) -D -m 0755 $(@D)/scripts/suifleet-portable-signer $(TARGET_DIR)/usr/sbin/suifleet-portable-signer
endef

define SUIFLEET_INSTALL_INIT_SYSV
	$(INSTALL) -D -m 0755 $(SUIFLEET_PKGDIR)/S95suifleet $(TARGET_DIR)/etc/init.d/S95suifleet
endef

$(eval $(cargo-package))
