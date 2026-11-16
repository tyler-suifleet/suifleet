"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DAppKitProvider, useWalletConnection } from "@mysten/dapp-kit-react";
import dynamic from "next/dynamic";
import { dAppKit } from "@/lib/dapp-kit";
import { usePathname } from "next/navigation";
import { FleetSelectionProvider, FleetScope } from "@/hooks/useFleetSelection";
import { FleetSwitcher } from "@/components/FleetSwitcher";
import { NETWORK } from "@/lib/constants";
import "./globals.css";
import "@/components/OrganizationSecurity.css";
import { NAV, MobileNav, Sidebar, isActive } from "@/components/ConsoleNavigation";
import { SubscriptionWarnings } from "@/components/SubscriptionWarnings";
import { HistoryStatus } from "@/components/HistoryStatus";
import { PageHeader, ConnectionRequired } from "@/components/ui";
import { useHydrated } from "@/hooks/useCurrentAccount";

// dApp Kit UI registers custom elements and must only load in the browser.
const ConnectButton = dynamic(() => import("@mysten/dapp-kit-react/ui").then((ui) => ui.ConnectButton), { ssr: false });
const queryClient = new QueryClient();

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <html lang="en">
      <head>
        <title>SuiFleet</title>
        <link rel="icon" href="/favicon.svg?v=400d0317ed09" type="image/svg+xml" sizes="any" />
        <link rel="alternate icon" href="/favicon.ico?v=400d0317ed09" sizes="16x16 32x32 48x48 64x64" />
        <meta name="description" content="Deploy software, manage device fleets and review verified update results." />
        <meta property="og:title" content="SuiFleet console" />
        <meta property="og:description" content="Manage edge fleets on Sui. Public testnet preview." />
        <meta property="og:type" content="website" />
        <meta property="og:url" content="https://app.suifleet.io/" />
        <link rel="canonical" href={"https://app.suifleet.io" + pathname} />
      </head>
      <body className="min-h-screen bg-slate-50 text-slate-900 flex flex-col antialiased">
        <a href="#main-content" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-lg focus:bg-white focus:p-3 focus:shadow-lg">Skip to content</a>
        <QueryClientProvider client={queryClient}>
          <DAppKitProvider dAppKit={dAppKit}>
            <FleetSelectionProvider>
              {/* Top bar */}
              <div className="bg-white text-slate-900 min-h-16 flex items-center px-4 md:px-6 gap-3 shrink-0 z-20 border-b border-slate-200">
                <div className="flex items-center gap-2">
                  <img src="/suifleet-mark.svg" alt="" width="42" height="26" className="h-[26px] w-[42px] shrink-0" />
                  <span className="font-semibold text-sm tracking-tight">SuiFleet</span>
                </div>
                <span className="hidden sm:inline text-slate-300 text-xs">|</span>
                <span className="hidden sm:inline text-slate-500 text-xs">Edge Fleet Management</span>
                <span className="ml-auto hidden sm:inline-flex rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-medium capitalize text-slate-600">{NETWORK}</span>
                <div className="ml-auto sm:ml-0 shrink-0">
                  <ConnectButton />
                </div>
              </div>

              <MobileNav />
              <div className="flex flex-1 overflow-hidden">
                <Sidebar />
                <main id="main-content" className="fleet-content flex-1 min-w-0 p-4 md:p-8 lg:px-10">
                  <div className="mx-auto w-full max-w-7xl">
                    <FleetSwitcher />
                    <SubscriptionWarnings />
                    <SignedOutGate>{children}</SignedOutGate>
                    <HistoryStatus />
                  </div>
                </main>
              </div>
            </FleetSelectionProvider>
            </DAppKitProvider>
        </QueryClientProvider>
      </body>
    </html>
  );
}

// Fleet pages read the connected account; signed out they would show empty
// totals or account errors. Get started carries its own sign-in step, so it
// renders signed out without waiting for a fleet selection. A saved wallet can
// reconnect before hydration, so the server and hydration renders show the
// sign-in check and only the mounted client trusts the live connection.
function SignedOutGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname(), connection = useWalletConnection(), hydrated = useHydrated();
  const route = NAV.find(item => item.href !== "/get-started" && isActive(item.href, pathname));
  if (pathname === "/billing" || pathname === "/billing/") return <>{children}</>;
  if (hydrated && connection.account) return <FleetScope allowWithoutFleet={pathname === "/entitlements"} pendingHeader={<PageHeader title={NAV.find(item => isActive(item.href, pathname))?.label ?? "Your fleet"} />}>{children}</FleetScope>;
  if (!route) return <>{children}</>;
  if (!hydrated || connection.isConnecting || connection.isReconnecting) return <p role="status">Checking your sign-in…</p>;
  return <ConnectionRequired title={route.label} description={route.description} />;
}
