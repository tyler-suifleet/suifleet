// UI-only boundary: real security components and factor metadata, controlled reads/wallet.
import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { OrganizationSecurity } from "@/components/OrganizationSecurity";
export * from "../../dapp/src/lib/security-policy";
export * from "../../dapp/src/lib/security-delay";
const scenario = new URLSearchParams(location.search).get("scenario") ?? "configured";
const fleet = { customerId: "0x" + "c".repeat(64), customerName: "Northstar Manufacturing", administrator: true };
const account = { address: "0x" + "a".repeat(64), chains: ["sui:localnet"] };
const client = {};
export const useFleetSelection = () => ({ fleet: scenario === "unselected" ? undefined : fleet, identity: "security-fixture" });
export const useCurrentClient = () => client;
export const useCurrentAccount = () => account;
export const useDAppKit = () => ({});
export const useFleetTransaction = () => ({ isPending: false, mutateAsync: async () => { throw Error("No transactions permitted in this visual fixture"); } });
export const securitySupported = () => scenario !== "unsupported";
export const delaySupported = () => scenario !== "unsupported";
export const fetchOrganizationSecurity = async () => {
  if (scenario === "loading") return new Promise(() => {});
  if (scenario === "error") throw Error("Fixture connection failure");
  if (scenario === "legacy") return undefined;
  return { generation: BigInt(3), requirements: ["off", "no-delays"].includes(scenario) ? [0,0,0,0,0,0,0] : [9,8,0,0,0,0,0] };
};
export const fetchSecurityDelays = async () => scenario === "no-delays" ? undefined : Array(7).fill(60000);
export const fetchQueuedSecurityActions = async () => [];
createRoot(document.getElementById("root")).render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><main className="mx-auto max-w-4xl p-5"><OrganizationSecurity /></main></QueryClientProvider>);
