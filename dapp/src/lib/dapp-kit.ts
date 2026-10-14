"use client";

import { createDAppKit } from "@mysten/dapp-kit-react";
import { SuiGrpcClient } from "@mysten/sui/grpc";
import { NETWORK, RPC_URL } from "./constants";

// The manifest selects one fleet/network. No silent fallback to a wallet's network.
export const dAppKit = createDAppKit({
  networks: [NETWORK],
  defaultNetwork: NETWORK,
  createClient: (network) => new SuiGrpcClient({
    network,
    baseUrl: RPC_URL ?? (network === "localnet" ? "http://127.0.0.1:9000" : `https://fullnode.${network}.sui.io:443`),
  }),
  autoConnect: true,
  slushWalletConfig: null,
});

declare module "@mysten/dapp-kit-react" {
  interface Register { dAppKit: typeof dAppKit; }
}
