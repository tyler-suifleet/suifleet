"use client";
import { StreamRetentionPanel } from "@/components/StreamRetention";

import { PageHeader, Section } from "@/components/ui";
import { FleetSettingsPanel } from "@/components/FleetSettings";

export default function SettingsPage() {
  return <div className="max-w-5xl space-y-4">
    <PageHeader title="Fleet settings" subtitle="Choose how often devices check, report and archive. Each option shows its network cost and SuiFleet price before you save." />
    <StreamRetentionPanel />
    <Section title="Cost settings"><FleetSettingsPanel /></Section>
  </div>;
}
