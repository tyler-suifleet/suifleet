"use client";

import { useDevices } from "@/hooks/useDevices";
import { useDeployments } from "@/hooks/useDeployments";
import { useGroups } from "@/hooks/useGroups";
import { useFleetHealth } from "@/hooks/useFleetHealth";
import { useFleetAlerts } from "@/hooks/useFleetAlerts";
import Link from "next/link";
import { PageHeader, Section, StatusDot } from "@/components/ui";

export default function DashboardPage() {
  const { data: devices = [], isLoading: dl } = useDevices();
  const { data: deployments = [], isLoading: depl } = useDeployments();
  const { data: groups = [], isLoading: gl } = useGroups();
  const { data: health = {} } = useFleetHealth(devices.filter((d) => d.ownerCapId).map((d) => d.objectId));
  const healthy = Object.values(health).filter((h) => h.status === "healthy").length;
  const reported = Object.values(health).some((h) => h.status !== "unavailable");

  const pending = deployments.filter((d) => d.resolvedDeviceIds.some((id) => (d.statuses[id] ?? 0) === 0));
  const alerts = useFleetAlerts();

  return (
    <div className="space-y-6 max-w-5xl">
      <PageHeader title="Dashboard" />
      <Link href="/get-started" className="block text-sm text-blue-700 underline">New here? Sign in and register your first device</Link>

      {/* Stat cards */}
      <div className="grid grid-cols-4 gap-4">
        <StatCard label="Devices (healthy)" value={dl ? "—" : reported ? `${devices.length} (${healthy})` : devices.length} href="/devices" />
        <StatCard label="Device groups" value={gl ? "—" : groups.length} href="/devices" />
        <StatCard label="Deployments" value={depl ? "—" : deployments.length} href="/deployments" />
        <StatCard label="Active deployments" value={depl ? "—" : pending.length} href="/deployments" />
      </div>

      {/* Alerts (SF-020): derived by the fleet indexer from verified telemetry and receipts */}
      <Section title="Alerts">
        {alerts.status === "unavailable" ? (
          <p className="text-sm text-gray-400">Alerts unavailable: {alerts.reason}. Enter the indexer read token on the Devices page.</p>
        ) : alerts.items.length === 0 ? (
          <p className="text-sm text-gray-500">No alerts for your devices.</p>
        ) : (
          <ul className="divide-y divide-gray-100 text-sm">
            {alerts.items.slice(0, 20).map((a) => (
              <li key={a.key} className="py-2">
                <span className={`mr-2 rounded px-1.5 py-0.5 text-xs font-medium ${a.severity === "critical" ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700"}`}>
                  {a.severity}
                </span>
                <Link href="/devices" className="font-medium hover:underline">{a.deviceName}</Link>
                <span className="text-gray-600"> — {a.message}</span>
                <p className="text-xs text-gray-400 mt-0.5">{a.action}</p>
              </li>
            ))}
            {alerts.items.length > 20 && <li className="py-2 text-xs text-gray-400">{alerts.items.length - 20} more (suifleet-indexer ctl alerts)</li>}
          </ul>
        )}
        <p className="mt-2 text-xs text-gray-400">Reported by the fleet indexer.</p>
      </Section>

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
                  <td className="py-3 pr-4 font-medium">{d.componentName}</td>
                  <td className="py-3 pr-4 text-gray-500">{d.version}</td>
                  <td className="py-3 pr-4 text-gray-500">{d.targetGroups.length + d.targetDevices.length}</td>
                  <td className="py-3 text-gray-400 text-xs">{new Date(d.timestampMs).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      {/* Device groups */}
      <Section title="Device groups" action={{ label: "Manage", href: "/devices" }}>
        {gl ? (
          <TableSkeleton cols={3} rows={2} />
        ) : groups.length === 0 ? (
          <Empty msg="No device groups yet." cta={{ label: "Create a group", href: "/devices" }} />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-200 text-left text-xs text-gray-500 uppercase">
                <Th>Name</Th>
                <Th>Description</Th>
                <Th>Devices</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {groups.map((g) => (
                <tr key={g.objectId} className="hover:bg-gray-50">
                  <td className="py-3 pr-4 font-medium">{g.name}</td>
                  <td className="py-3 pr-4 text-gray-500 text-xs">{g.description || "—"}</td>
                  <td className="py-3 text-gray-500">{g.deviceIds.length}</td>
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
                    <StatusDot health={health[d.objectId]} />
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
