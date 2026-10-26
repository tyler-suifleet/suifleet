"use client";

import { PageHeader, Section, InfoCard } from "@/components/ui";
import { FleetSettingsPanel } from "@/components/FleetSettings";

export default function SettingsPage() {
  return <div className="max-w-5xl space-y-4">
    <PageHeader title="Fleet settings" subtitle="Choose when devices check for updates, send health reports and upload data." />
    <InfoCard title="What happens when I save?">
      <p>Your settings are recorded securely on the Sui network, alongside your fleet budget. Devices read them at their next check. Your account approval app shows the network fee for saving.</p>
      <p><strong>Saving settings does not buy a subscription or add money to your budget.</strong> Fund device activity and storage separately on the Network budget page. Estimates below are planning tools, not a checkout.</p>
    </InfoCard>
    <Section title="Device reporting and storage"><FleetSettingsPanel /></Section>
  </div>;
}
