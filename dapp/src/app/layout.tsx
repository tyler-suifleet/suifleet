"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SuiClientProvider, WalletProvider, ConnectButton } from "@mysten/dapp-kit";
import { getFullnodeUrl } from "@mysten/sui/client";
import { usePathname } from "next/navigation";
import { NETWORK } from "@/lib/constants";
import "@mysten/dapp-kit/dist/index.css";
import "./globals.css";
import Link from "next/link";

const queryClient = new QueryClient();
const networks = {
  devnet:  { url: getFullnodeUrl("devnet") },
  testnet: { url: getFullnodeUrl("testnet") },
  mainnet: { url: getFullnodeUrl("mainnet") },
};

const NAV = [
  { href: "/",            label: "Dashboard" },
  { href: "/devices",     label: "Devices" },
  { href: "/deployments", label: "Deployments" },
  { href: "/firmware",    label: "Deploy component" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <title>SuiFleet</title>
        <meta name="description" content="Decentralized edge fleet management on Sui" />
      </head>
      <body className="min-h-screen bg-[#f0f2f5] text-gray-900 flex flex-col">
        <QueryClientProvider client={queryClient}>
          <SuiClientProvider networks={networks} defaultNetwork={NETWORK}>
            <WalletProvider autoConnect>
              {/* Top bar */}
              <div className="bg-[#0b1628] text-white h-11 flex items-center px-5 gap-3 shrink-0 z-20 border-b border-white/5">
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
                <span className="text-white/20 text-xs">|</span>
                <span className="text-white/40 text-xs">Edge Fleet Management</span>
                <div className="ml-auto">
                  <ConnectButton />
                </div>
              </div>

              <div className="flex flex-1 overflow-hidden">
                <Sidebar />
                <main className="flex-1 overflow-auto p-8">
                  {children}
                </main>
              </div>
            </WalletProvider>
          </SuiClientProvider>
        </QueryClientProvider>
      </body>
    </html>
  );
}

function Sidebar() {
  const pathname = usePathname();
  return (
    <aside className="w-52 bg-[#0b1628] shrink-0 flex flex-col z-10 border-r border-white/5">
      <div className="px-3 pt-5 pb-3">
        <p className="text-[10px] font-semibold text-white/30 uppercase tracking-widest px-1">Manage</p>
      </div>
      <nav className="flex-1 space-y-0.5 px-2">
        {NAV.map(({ href, label }) => {
          const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              className={`flex items-center px-3 py-2 text-sm rounded transition-colors ${
                active
                  ? "bg-[#4DA2FF]/15 text-[#4DA2FF] font-medium"
                  : "text-white/50 hover:bg-white/5 hover:text-white/80"
              }`}
            >
              {label}
            </Link>
          );
        })}
      </nav>
      <div className="px-4 py-4 border-t border-white/5">
        <p className="text-[10px] text-white/25">
          Network: <span className="text-white/40">{NETWORK}</span>
        </p>
      </div>
    </aside>
  );
}
