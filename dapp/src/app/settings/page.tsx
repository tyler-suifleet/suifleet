"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { StreamRetentionPanel } from "@/components/StreamRetention";
import { PageHeader, Section } from "@/components/ui";
import { OrganizationSecurity } from "@/components/OrganizationSecurity";
import { FleetSettingsPanel, StorageEndpoints } from "@/components/FleetSettings";

const SECTIONS = [
  { id: "reporting", title: "Device reporting", description: "Updates and health checks" },
  { id: "storage", title: "Data retention", description: "How long device data is kept" },
  { id: "security", title: "Security", description: "Organization access and protection" },
  { id: "advanced", title: "Advanced", description: "Storage connections and history" },
] as const;
type SettingsSection = typeof SECTIONS[number]["id"];

export default function SettingsPage() {
  const [active, setActive] = useState<SettingsSection>("reporting");
  useEffect(() => {
    const showLinkedSection = () => {
      if (window.location.hash === "#connection-history") setActive("advanced");
    };
    showLinkedSection();
    window.addEventListener("hashchange", showLinkedSection);
    return () => window.removeEventListener("hashchange", showLinkedSection);
  }, []);
  return <div className="max-w-6xl space-y-6">
    <PageHeader title="Fleet settings" subtitle="Manage how your fleet checks in, stores data and stays protected." />
    <div className="grid items-start gap-6 lg:grid-cols-[220px_minmax(0,1fr)]">
      <nav aria-label="Settings sections" className="grid grid-cols-2 gap-2 rounded-2xl border border-slate-200 bg-white p-2 shadow-sm lg:sticky lg:top-6 lg:grid-cols-1">
        {SECTIONS.map(section => <button key={section.id} type="button" aria-pressed={active === section.id}
          aria-controls={`settings-${section.id}`} onClick={() => setActive(section.id)}
          className={`rounded-xl px-3 py-3 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 ${active === section.id ? "bg-blue-50 text-blue-800 ring-1 ring-blue-100" : "text-slate-600 hover:bg-slate-50"}`}>
          <span className="block text-sm font-semibold">{section.title}</span>
          <span className={`mt-1 block text-xs leading-relaxed ${active === section.id ? "text-blue-700" : "text-slate-500"}`}>{section.description}</span>
        </button>)}
      </nav>
      <div className="min-w-0">
        {/* Keep panels mounted so navigating never discards an unsaved form. */}
        <section id="settings-reporting" aria-label="Device reporting settings" hidden={active !== "reporting"} className="space-y-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div><h2 className="text-lg font-semibold tracking-tight text-slate-900">Device reporting</h2>
              <p className="mt-1 text-sm leading-relaxed text-slate-500">Set the rhythm for software checks and health reports.</p></div>
            <Link href="/budget/" className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50">View network budget <span aria-hidden="true">↗</span></Link>
          </div>
          <FleetSettingsPanel />
        </section>
        <section id="settings-storage" aria-label="Data retention settings" hidden={active !== "storage"} className="space-y-5">
          <div><h2 className="text-lg font-semibold tracking-tight text-slate-900">Data retention</h2>
            <p className="mt-1 text-sm leading-relaxed text-slate-500">Set storage periods for different types of device data. Shared archive defaults are under Device reporting.</p></div>
          <StreamRetentionPanel />
        </section>
        <section id="settings-security" aria-label="Security settings" hidden={active !== "security"}>
          <Section title="Organization security"><OrganizationSecurity /></Section>
        </section>
        <section id="settings-advanced" aria-label="Advanced settings" hidden={active !== "advanced"} className="space-y-5">
          <div><h2 className="text-lg font-semibold tracking-tight text-slate-900">Advanced</h2>
            <p className="mt-1 text-sm leading-relaxed text-slate-500">Connection options for troubleshooting. Most fleets can use the defaults.</p></div>
          <StorageEndpoints />
          <section id="connection-history" className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <h3 className="text-sm font-semibold text-slate-900">Activity history</h3>
            <p className="mt-2 text-sm leading-relaxed text-slate-600">Your connection may not provide older activity. Current fleet settings and stored update decisions remain available.</p>
            <details className="mt-4 text-sm text-slate-600"><summary className="cursor-pointer font-medium text-slate-700">Connection history details</summary>
              <div className="mt-3 space-y-3 leading-relaxed">
                <p>Public Sui connections can prune old events. This does not delete current objects or stored rollout decisions. New decisions remain readable from current chain state while the rollout exists.</p>
                <p>Older rollout revisions may have only an event. The audit tries retained events and lists the exact unreadable revisions. A complete audit is labeled complete; a pruned interval is never treated as empty history. An archival connection can recover legacy events, but is unnecessary for stored decisions.</p>
                <p>Device, group and deployment discovery can still depend on retained events and browser storage. Keep known object IDs and fleet indexes; changing connections cannot recreate events the provider no longer retains.</p>
              </div>
            </details>
          </section>
        </section>
      </div>
    </div>
  </div>;
}
