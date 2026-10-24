"use client";

import { fleetFetch } from "./rpc-budget";

import { createDAppKit } from "@mysten/dapp-kit-react";
import { enokiWalletsInitializer } from "@mysten/enoki";
import { onboardingConfig } from "./onboarding";
import { SuiGrpcClient } from "@mysten/sui/grpc";
import { NETWORK, RPC_URL } from "./constants";

const apiKey = process.env.NEXT_PUBLIC_ENOKI_API_KEY;
const googleClientId = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID;
const onboarding = onboardingConfig(NETWORK, apiKey, googleClientId);

// The manifest selects one fleet/network. No silent fallback to a wallet's network.
export const dAppKit = createDAppKit({
  networks: [NETWORK],
  defaultNetwork: NETWORK,
  createClient: (network) => new SuiGrpcClient({
    network,
    fetch: fleetFetch,
    baseUrl: RPC_URL ?? (network === "localnet" ? "http://127.0.0.1:9000" : `https://fullnode.${network}.sui.io:443`),
  }),
  autoConnect: true,
  slushWalletConfig: typeof window === "undefined" ? null : { appName: "SuiFleet" },
  walletInitializers: typeof window !== "undefined" && onboarding.enabled ? [enokiWalletsInitializer({
    apiKey: apiKey!,
    providers: { google: { clientId: googleClientId!, redirectUrl: typeof window === "undefined" ? undefined : new URL("/get-started", window.location.origin).href } },
    additionalEpochs: 0,
  })] : [],
});

declare module "@mysten/dapp-kit-react" {
  interface Register { dAppKit: typeof dAppKit; }
}
