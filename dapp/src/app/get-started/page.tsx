"use client";

import dynamic from "next/dynamic";
import { FirstRun } from "@/components/FirstRun";
const ConnectButton = dynamic(() => import("@mysten/dapp-kit-react/ui").then((ui) => ui.ConnectButton), { ssr: false });

export default function GetStartedPage() {
  return <FirstRun connect={<ConnectButton />} />;
}
