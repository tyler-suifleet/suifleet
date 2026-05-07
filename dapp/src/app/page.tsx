"use client";

import { useDevices } from "@/hooks/useDevices";
import { useDeployments } from "@/hooks/useDeployments";
import Link from "next/link";

export default function DashboardPage() {
  const { data: devices = [], isLoading: dl } = useDevices();
  const { data: deployments = [], isLoading: depl } = useDeployments();

  const pending  = deployments.filter((d) => d.targetGroups.length > 0);

  return (
    <div className="space-y-6 max-w-5xl">
      <PageHeader title="Dashboard" />

      {/* Stat cards */}
      <div className="grid grid-cols-3 gap-4">
        <StatCard label="Devices" value={dl ? "—" : devices.length} href="/devices" />
        <StatCard label="Deployments" value={depl ? "—" : deployments.length} href="/deployments" />
        <StatCard label="Active deployments" value={depl ? "—" : pending.length} href="/deployments" />
      </div>

      {/* Recent deployments */}
      <Section title="Recent deployments" action={{ label: "View all", href: "/deployments" }}>
        {depl ? (
          <TableSkeleton cols={4} rows={3} />
        ) : deployments.length === 0 ? (
          <Empty msg="No deployments yet." cta={{ label: "Deploy firmware", href: "/firmware" }} />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-200 text-left text-xs text-gray-500 uppercase">
                <Th>Name</Th>
                <Th>Version</Th>
                <Th>Targets</Th>
                <Th>Created</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {deployments.slice(0, 6).map((d) => (
                <tr key={d.deploymentId} className="hover:bg-gray-50">
                  <td className="py-3 pr-4 font-medium">{d.firmwareName}</td>
                  <td className="py-3 pr-4 text-gray-500">{d.version}</td>
                  <td className="py-3 pr-4 text-gray-500">{d.targetGroups.length}</td>
                  <td className="py-3 text-gray-400 text-xs">{new Date(d.timestampMs).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      {/* Devices */}
      <Section title="Devices" action={{ label: "Manage", href: "/devices" }}>
        {dl ? (
          <TableSkeleton cols={3} rows={2} />
        ) : devices.length === 0 ? (
          <Empty msg="No devices registered." cta={{ label: "Register a device", href: "/devices" }} />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-200 text-left text-xs text-gray-500 uppercase">
                <Th>Name</Th>
                <Th>Architecture</Th>
                <Th>Object ID</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {devices.map((d) => (
                <tr key={d.objectId} className="hover:bg-gray-50">
                  <td className="py-3 pr-4 font-medium flex items-center gap-2">
                    <StatusDot healthy />
                    {d.name}
                  </td>
                  <td className="py-3 pr-4 text-gray-500">{d.arch}</td>
                  <td className="py-3 text-gray-400 font-mono text-xs">{d.objectId.slice(0, 20)}…</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    </div>
  );
}

// ── Shared components ──────────────────────────────────────────────────────────

export function PageHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mb-2">
      <h1 className="text-xl font-semibold text-gray-900">{title}</h1>
      {subtitle && <p className="text-sm text-gray-500 mt-0.5">{subtitle}</p>}
    </div>
  );
}

export function Section({
  title,
  action,
  children,
}: {
  title: string;
  action?: { label: string; href: string };
  children: React.ReactNode;
}) {
  return (
    <div className="bg-white border border-gray-200 rounded">
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200">
        <h2 className="text-sm font-semibold">{title}</h2>
        {action && (
          <Link href={action.href} className="text-xs text-[#4DA2FF] hover:underline">
            {action.label}
          </Link>
        )}
      </div>
      <div className="px-4 py-3">{children}</div>
    </div>
  );
}

export function StatusDot({ healthy }: { healthy: boolean }) {
  return (
    <span className={`inline-block w-2 h-2 rounded-full ${healthy ? "bg-green-500" : "bg-gray-400"}`} />
  );
}

function StatCard({ label, value, href }: { label: string; value: number | string; href: string }) {
  return (
    <Link href={href} className="bg-white border border-gray-200 rounded p-4 hover:border-[#4DA2FF] transition-colors block">
      <p className="text-xs text-gray-500 uppercase tracking-wide">{label}</p>
      <p className="text-3xl font-light mt-1 text-gray-800">{value}</p>
    </Link>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="py-2 pr-4 font-medium">{children}</th>;
}

function TableSkeleton({ cols, rows }: { cols: number; rows: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex gap-4">
          {Array.from({ length: cols }).map((_, j) => (
            <div key={j} className="h-4 bg-gray-100 rounded flex-1 animate-pulse" />
          ))}
        </div>
      ))}
    </div>
  );
}

function Empty({ msg, cta }: { msg: string; cta?: { label: string; href: string } }) {
  return (
    <div className="text-center py-8 text-sm text-gray-400">
      <p>{msg}</p>
      {cta && (
        <Link href={cta.href} className="mt-2 inline-block text-[#4DA2FF] hover:underline text-xs">
          {cta.label}
        </Link>
      )}
    </div>
  );
}
