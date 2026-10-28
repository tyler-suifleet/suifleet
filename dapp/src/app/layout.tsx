"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DAppKitProvider } from "@mysten/dapp-kit-react";
import dynamic from "next/dynamic";
import { dAppKit } from "@/lib/dapp-kit";
import { usePathname } from "next/navigation";
import { FleetSelectionProvider, FleetScope } from "@/hooks/useFleetSelection";
import { FleetSwitcher } from "@/components/FleetSwitcher";
import { NETWORK } from "@/lib/constants";
import "./globals.css";
import Link from "next/link";
import { HistoryStatus } from "@/components/HistoryStatus";

// dApp Kit UI registers custom elements and must only load in the browser.
const ConnectButton = dynamic(() => import("@mysten/dapp-kit-react/ui").then((ui) => ui.ConnectButton), { ssr: false });
const queryClient = new QueryClient();
const NAV = [
  { href: "/get-started", label: "Get started" },
  { href: "/",            label: "Dashboard" },
  { href: "/devices",     label: "Devices" },
  { href: "/services",    label: "Device services" },
  { href: "/data",        label: "Data" },
  { href: "/enrollment",  label: "Enrollment" },
  { href: "/deployments", label: "Deployments" },
  { href: "/rollouts",    label: "Rollouts" },
  { href: "/components", label: "Components" },
  { href: "/firmware",    label: "Deploy component" },
  { href: "/access",      label: "Access policies" },
  { href: "/handover",    label: "Administration" },
  { href: "/entitlements", label: "Device entitlements" },
  { href: "/settings",    label: "Fleet settings" },
  { href: "/budget",      label: "Network budget" },
];
const NAV_GROUPS = [
  { label: "Workspace", routes: ["/", "/get-started"] },
  { label: "Fleet operations", routes: ["/devices", "/services", "/deployments", "/rollouts", "/components", "/firmware", "/data"] },
  { label: "Administration", routes: ["/enrollment", "/access", "/handover", "/entitlements", "/settings", "/budget"] },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <html lang="en">
      <head>
        <title>SuiFleet</title>
        <meta name="description" content="Decentralized edge fleet management on Sui" />
      </head>
      <body className="min-h-screen bg-slate-50 text-slate-900 flex flex-col antialiased">
        <a href="#main-content" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-lg focus:bg-white focus:p-3 focus:shadow-lg">Skip to content</a>
        <QueryClientProvider client={queryClient}>
          <DAppKitProvider dAppKit={dAppKit}>
            <FleetSelectionProvider>
              {/* Top bar */}
              <div className="bg-white text-slate-900 min-h-16 flex items-center px-4 md:px-6 gap-3 shrink-0 z-20 border-b border-slate-200">
                <div className="flex items-center gap-2">
                  <div className="w-6 h-6 rounded bg-[#4DA2FF] flex items-center justify-center shrink-0">
                    <svg viewBox="0 0 16 16" fill="none" className="w-3.5 h-3.5">
                      <circle cx="8" cy="4.5" r="2.2" fill="white" />
                      <circle cx="3" cy="12" r="2" fill="white" />
                      <circle cx="13" cy="12" r="2" fill="white" />
                      <line x1="8" y1="6.7" x2="3.8" y2="10.2" stroke="white" strokeWidth="1.2" />
                      <line x1="8" y1="6.7" x2="12.2" y2="10.2" stroke="white" strokeWidth="1.2" />
                    </svg>
                  </div>
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
                    <FleetScope allowWithoutFleet={pathname === "/entitlements"}>{children}</FleetScope>
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

// Narrow screens: the same navigation as a horizontally scrolling bar.
function MobileNav() {
  const pathname = usePathname();
  return (
    <nav aria-label="Main" className="md:hidden bg-[#0b1628] border-b border-white/5 overflow-x-auto">
      <ul className="flex gap-1 px-2 py-1.5 whitespace-nowrap">
        {NAV.map(({ href, label }) => {
          const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
          return (
            <li key={href}>
              <Link href={href} aria-current={active ? "page" : undefined}
                className={`block px-3 py-1.5 text-sm rounded ${active ? "bg-[#4DA2FF]/15 text-[#4DA2FF] font-medium" : "text-white/70 hover:text-white"}`}>
                {label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

function Sidebar() {
  const pathname = usePathname();
  return (
    <aside className="hidden md:flex w-60 bg-[#0b1628] shrink-0 flex-col border-r border-slate-800">
      <nav aria-label="Main" className="flex-1 space-y-6 px-3 py-6">
        {NAV_GROUPS.map(group => <div key={group.label}>
          <p className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-widest text-slate-400">{group.label}</p>
          <ul className="space-y-1">{group.routes.map(href => {
            const item = NAV.find(item => item.href === href)!;
            const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
            return <li key={href}><Link href={href} aria-current={active ? "page" : undefined}
              className={`flex min-h-10 items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${active
                ? "bg-blue-500/15 text-blue-200 font-semibold ring-1 ring-blue-400/20"
                : "text-slate-300 hover:bg-white/5 hover:text-white"}`}>
              <span aria-hidden="true" className={`h-1.5 w-1.5 shrink-0 rounded-full ${active ? "bg-blue-400" : "bg-slate-600"}`} />{item.label}
            </Link></li>;
          })}</ul>
        </div>)}
      </nav>
      <div className="border-t border-white/10 px-6 py-5 text-xs text-slate-400">
        Edge fleet management <span className="mt-1 block capitalize">{NETWORK} workspace</span>
      </div>
    </aside>
  );
}
