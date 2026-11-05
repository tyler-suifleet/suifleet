// Public documentation paths are stable across source repository moves.
// Override at build time when serving the curated docs on another origin/path.
export const DOCS_BASE = (process.env.NEXT_PUBLIC_DEVICE_DOCS_BASE || "https://docs.suifleet.io/").replace(/\/?$/, "/");
export type DocsPage = "install" | "enrollment" | "components" | "budget" | "pricing";
/** All customer help links use the same configured documentation origin. */
export function docsUrl(page: DocsPage) { return `${DOCS_BASE}${page}/`; }
export const GUIDE_FILE = "device-client/";

/** Guide sections, by heading anchor. */
export const GUIDE_SECTIONS = {
  platforms: "get-the-client",
  yocto: "yocto-image-and-recipe",
  macos: "macos-package",
  linux: "generic-linux",
  debian: "debian-and-ubuntu",
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
  { section: "debian", label: "Debian / Ubuntu", note: "package availability and Linux installation steps" },
  { section: "linux", label: "Other Linux with systemd", note: "same files as the Yocto recipe, installed by hand" },
];
