"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { NETWORK } from "@/lib/constants";
export const NAV = [
  { href: "/get-started", label: "Get started", description: "Connect your account, prepare a device and send its first update." },
  { href: "/",            label: "Dashboard", description: "See device health, recent updates and active incidents across your fleet." },
  { href: "/devices",     label: "Devices", description: "Manage your devices and device groups." },
  { href: "/services",    label: "Device services", description: "See how applications on a device are communicating, storing data and staying in sync." },
  { href: "/data",        label: "Data", description: "Browse device data, download verified files and manage scoped sharing." },
  { href: "/enrollment",  label: "Enrollment", description: "Prepare device batches and securely enroll devices with single-use credentials." },
  { href: "/deployments", label: "Deployments", description: "Review device update history, outcomes and deployment controls." },
  { href: "/rollouts",    label: "Rollouts", description: "Send updates to a small group first, then expand once device health checks pass." },
  { href: "/components", label: "Software library", description: "Browse your software library and publish component versions." },
  { href: "/firmware",    label: "Deploy software", description: "Choose software, select your devices and review an update before deploying." },
  { href: "/access",      label: "Access policies", description: "Choose which devices and accounts can open private software releases." },
  { href: "/handover",    label: "Administration", description: "Transfer control of selected devices and resources to another administrator." },
  { href: "/entitlements", label: "Device capacity", description: "Review how many devices your organization can manage and how to increase that limit." },
  { href: "/settings",    label: "Fleet settings", description: "Choose when devices check for updates, send health reports and upload data." },
  { href: "/billing", label: "Plans and billing", description: "Choose capacity, prepay subscriptions and download receipts." },
  { href: "/budget",      label: "Network budget", description: "Keep devices funded for activity and storage, with spending limits you control." },
];
const NAV_GROUPS = [
  { label: "Workspace", routes: ["/", "/get-started"] },
  { label: "Fleet operations", routes: ["/devices", "/services", "/deployments", "/rollouts", "/components", "/firmware", "/data"] },
  { label: "Administration", routes: ["/enrollment", "/access", "/handover", "/entitlements", "/settings", "/budget", "/billing"] },
];

export const isActive = (href: string, pathname: string) => href === "/" ? pathname === "/" : pathname.startsWith(href);

// A labelled, grouped menu exposes every route without horizontal scrolling.
export function MobileNav() {
  const pathname = usePathname();
  return <details key={pathname} className="md:hidden bg-[#0b1628] text-white">
    <summary className="cursor-pointer p-4 font-medium">All sections · {NAV.find(item => isActive(item.href, pathname))?.label ?? "Console"}</summary>
    <nav aria-label="Main" className="space-y-4 px-4 pb-4">
      {NAV_GROUPS.map(group => <div key={group.label}>
        <p className="text-xs uppercase tracking-wide text-slate-400">{group.label}</p>
        <ul>{group.routes.map(href => {
          const item = NAV.find(item => item.href === href)!;
          return <li key={href}><Link href={href} aria-current={isActive(href, pathname) ? "page" : undefined} className="block rounded py-3 text-sm hover:bg-white/10">
            <span className="font-semibold">{item.label}</span><span className="mt-1 block text-xs text-slate-300">{item.description}</span>
          </Link></li>;
        })}</ul>
      </div>)}
    </nav>
  </details>;
}

export function Sidebar() {
  const pathname = usePathname();
  return (
    <aside className="hidden md:flex w-60 bg-[#0b1628] shrink-0 flex-col border-r border-slate-800">
      <nav aria-label="Main" className="flex-1 space-y-6 px-3 py-6">
        {NAV_GROUPS.map(group => <div key={group.label}>
          <p className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-widest text-slate-400">{group.label}</p>
          <ul className="space-y-1">{group.routes.map(href => {
            const item = NAV.find(item => item.href === href)!;
            const active = isActive(href, pathname);
            return <li key={href}><Link href={href} aria-current={active ? "page" : undefined} title={item.description}
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
