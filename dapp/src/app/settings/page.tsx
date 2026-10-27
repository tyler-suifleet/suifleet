"use client";
import { StreamRetentionPanel } from "@/components/StreamRetention";

import { PageHeader, Section, InfoCard } from "@/components/ui";
import { FleetSettingsPanel } from "@/components/FleetSettings";

export default function SettingsPage() {
  return <div className="max-w-5xl space-y-4">
    <PageHeader title="Fleet settings" subtitle="Choose when devices check for updates, send health reports and upload data." />
    <InfoCard title="What happens when I save?">
      <p>Your settings are recorded securely on the Sui network, alongside your fleet budget. Devices read them at their next check. Your account approval app shows the network fee for saving.</p>
      <p><strong>Saving settings does not buy a subscription or add money to your budget.</strong> Fund device activity and storage separately on the Network budget page. Estimates below are planning tools, not a checkout.</p>
    </InfoCard>
    <section id="connection-history" className="rounded-lg border p-4 space-y-2">
      <h2>Connection history</h2>
      <p>Public Sui connections can prune old events. This does not delete current objects or stored rollout decisions. New decisions remain readable from current chain state while the rollout exists.</p>
      <p>Older rollout revisions may have only an event. The audit tries retained events and lists the exact unreadable revisions. A complete audit is labeled complete; a pruned interval is never treated as empty history. An archival connection can recover legacy events, but is unnecessary for stored decisions.</p>
      <p>Device, group and deployment discovery can still depend on retained events and browser storage. Keep known object IDs and fleet indexes; changing connections cannot recreate events the provider no longer retains.</p>
    </section>
    <StreamRetentionPanel />
    <Section title="Device reporting and storage"><FleetSettingsPanel /></Section>
  </div>;
}
