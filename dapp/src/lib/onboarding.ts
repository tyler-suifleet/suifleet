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
  if (!isValidSuiAddress(deviceAddress)) throw new Error("Enter the full device address the client printed (0x and 64 hex characters), not your owner wallet address.");
  if (!["aarch64", "armv7", "x86_64"].includes(arch)) throw new Error("Choose a supported architecture.");
  return { name: name.trim(), deviceAddress: normalizeSuiAddress(deviceAddress), arch };
}

const MIST_PER_SUI = BigInt(1_000_000_000);

function groupDigits(value: bigint) {
  return value.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** Exact SUI amount for a MIST balance, without floating-point rounding. */
export function formatSui(mist: string | bigint) {
  const value = BigInt(mist);
  if (value < BigInt(0)) throw new Error("Balance cannot be negative.");
  const whole = value / MIST_PER_SUI, fraction = (value % MIST_PER_SUI).toString().padStart(9, "0").replace(/0+$/, "");
  return `${groupDigits(whole)}${fraction ? `.${fraction}` : ""} SUI`;
}

/** Readable SUI balance rounded down to four decimals; never shows a nonzero balance as zero. */
export function displaySui(mist: string | bigint) {
  const value = BigInt(mist), step = MIST_PER_SUI / BigInt(10_000);
  if (value > BigInt(0) && value < step) return "< 0.0001 SUI";
  return formatSui(value - (value % step));
}

export function shortAddress(address: string) {
  return address.length > 14 ? `${address.slice(0, 6)}…${address.slice(-4)}` : address;
}

export type StepState = "done" | "current" | "blocked" | "upcoming";
export interface OnboardingStep { id: "account" | "funding" | "register" | "update"; title: string; state: StepState; status: string }
type Loaded<T> = { status: "pending" } | { status: "error" } | { status: "success"; value: T };

/**
 * Honest onboarding progress. Registration is an on-chain inventory fact; it does
 * not mean the daemon is online or that any software is installed, so the
 * first-update step is never reported done here.
 */
export function onboardingProgress(connected: boolean, balance: Loaded<string>, devices: Loaded<number>): OnboardingStep[] {
  const funded = balance.status === "success" && BigInt(balance.value) > BigInt(0);
  const registered = devices.status === "success" && devices.value > 0;
  const at = (state: StepState, status: string) => ({ state, status });
  const account: OnboardingStep = { id: "account", title: "Sign in", ...(connected ? at("done", "Connected") : at("current", "Not connected")) };
  const funding: OnboardingStep = { id: "funding", title: "Add funds", ...(!connected ? at("upcoming", "Waiting for sign-in")
    : balance.status === "pending" ? at("current", "Checking balance…")
    : balance.status === "error" ? at("blocked", "Balance unavailable")
    : funded ? at("done", "Funds available") : at("current", "Not funded")) };
  const register: OnboardingStep = { id: "register", title: "Register a device", ...(!connected ? at("upcoming", "Waiting for sign-in")
    : devices.status === "error" ? at("blocked", "Inventory unavailable")
    : devices.status === "pending" ? at(funded ? "current" : "upcoming", "Checking inventory…")
    : registered ? at("done", `${devices.value} registered`)
    : funded ? at("current", "No devices yet") : at("upcoming", "Needs funds first")) };
  const update: OnboardingStep = { id: "update", title: "Deploy a first update", ...(registered
    ? at("current", "Ready to deploy") : at("upcoming", "Needs a registered device")) };
  return [account, funding, register, update];
}
