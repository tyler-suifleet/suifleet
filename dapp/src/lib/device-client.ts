// Where operators get, install and configure the device client (SF-169).
// Repository documentation until the SF-172 documentation site exists;
// scripts/test_device_client_links.cjs checks every link and command here.

export const DOCS_BASE = "https://github.com/tyler-suifleet/suifleet/blob/main/docs/";
export const GUIDE_FILE = "INSTALL_DEVICE_CLIENT.md";

/** Guide sections, by heading anchor. */
export const GUIDE_SECTIONS = {
  platforms: "get-the-client",
  yocto: "yocto-image-and-recipe",
  macos: "macos-package",
  linux: "generic-linux",
  address: "find-the-device-address",
  manifest: "network-manifest-deploymentjson",
  signer: "signing-service-configuration-signertoml",
  config: "configuration-file",
  enroll: "first-boot-enrollment",
  register: "register-by-address",
} as const;
export type GuideSection = keyof typeof GUIDE_SECTIONS;

export function guideUrl(section?: GuideSection) {
  return `${DOCS_BASE}${GUIDE_FILE}${section ? `#${GUIDE_SECTIONS[section]}` : ""}`;
}

/** The line provisioning prints on every platform, and the address it contains. */
export const ADDRESS_LINE = "SuiFleet device address: 0x…";
export const ADDRESS_FORMAT = "0x followed by 64 hexadecimal characters";

/** Short exact commands shown in the console; each is run on the device. */
export const COMMANDS = {
  readAddress: "sudo cat /etc/suifleet/device.address",
  provisionLog: "journalctl -u suifleet-provision.service",
  deriveAddress: "sudo suifleet --derive-address /etc/suifleet/device.key",
  enroll: "sudo suifleet --enroll /path/to/enroll.toml",
  checkConfig: "sudo suifleet --check-config /etc/suifleet/config.toml",
  status: "systemctl status suifleet.service",
} as const;

export const PLATFORMS: { section: GuideSection; label: string; note: string }[] = [
  { section: "yocto", label: "Raspberry Pi / Yocto image", note: "meta-suifleet layer; key generated at first boot" },
  { section: "macos", label: "macOS (Apple silicon)", note: "LaunchDaemon installer; prints the address" },
  { section: "linux", label: "Other Linux with systemd", note: "same files as the Yocto recipe, installed by hand" },
];
