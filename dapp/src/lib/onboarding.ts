import { isValidSuiAddress, normalizeSuiAddress } from "@mysten/sui/utils";

// Public identifiers only. Never accept a private Enoki API key in a browser build.
export function onboardingConfig(network: string, apiKey?: string, googleClientId?: string) {
  const supported = network === "testnet" || network === "mainnet";
  const enabled = supported && !!apiKey?.startsWith("enoki_public_") && !!googleClientId?.endsWith(".apps.googleusercontent.com");
  return { enabled, reason: !supported
    ? "Google sign-in is available on testnet and mainnet. For localnet, connect a localnet wallet."
    : "Google sign-in is not configured for this build. Use Slush web or an extension wallet." };
}

export function registrationInput(name: string, deviceAddress: string, arch: string) {
  if (!name.trim()) throw new Error("Enter a device name.");
  if (!isValidSuiAddress(deviceAddress)) throw new Error("Enter the full device address from the daemon, not your owner wallet address.");
  if (!["aarch64", "armv7", "x86_64"].includes(arch)) throw new Error("Choose a supported architecture.");
  return { name: name.trim(), deviceAddress: normalizeSuiAddress(deviceAddress), arch };
}
