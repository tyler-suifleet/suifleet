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
  { href: "/devices",     label: "Core devices" },
  { href: "/deployments", label: "Deployments" },
  { href: "/firmware",    label: "Deploy firmware" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-[#f2f3f3] text-gray-900 flex flex-col">
        <QueryClientProvider client={queryClient}>
          <SuiClientProvider networks={networks} defaultNetwork={NETWORK}>
            <WalletProvider autoConnect>
              {/* Top service bar */}
              <div className="bg-[#232f3e] text-white h-10 flex items-center px-4 gap-4 shrink-0 z-20">
                <span className="font-semibold text-sm tracking-wide">SUI Edge</span>
                <span className="text-gray-400 text-xs">|</span>
                <span className="text-gray-300 text-xs">IoT Greengrass</span>
                <div className="ml-auto">
                  <ConnectButton />
                </div>
              </div>

              <div className="flex flex-1 overflow-hidden">
                {/* Sidebar */}
                <Sidebar />

                {/* Page content */}
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
    <aside className="w-56 bg-[#1a2332] shrink-0 flex flex-col py-4 z-10">
      <div className="px-4 mb-6">
        <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-widest">Manage</p>
      </div>
      <nav className="flex-1 space-y-0.5">
        {NAV.map(({ href, label }) => {
          const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              className={`flex items-center px-4 py-2 text-sm transition-colors ${
                active
                  ? "bg-[#ec7211] text-white font-medium"
                  : "text-gray-300 hover:bg-[#273547] hover:text-white"
              }`}
            >
              {label}
            </Link>
          );
        })}
      </nav>
      <div className="px-4 pt-4 border-t border-gray-700">
        <p className="text-[10px] text-gray-500">
          Network: <span className="text-gray-400">{NETWORK}</span>
        </p>
      </div>
    </aside>
  );
}
