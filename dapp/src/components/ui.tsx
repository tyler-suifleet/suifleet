import Link from "next/link";
import type { DeviceHealth, FleetStatus } from "@/lib/telemetry";

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

const STATUS_STYLE: Record<FleetStatus, { dot: string; label: string }> = {
  healthy: { dot: "bg-green-500", label: "Healthy" },
  degraded: { dot: "bg-amber-500", label: "Degraded" },
  offline: { dot: "bg-red-500", label: "Offline" },
  unverified: { dot: "bg-red-500", label: "Unverified" },
  unreported: { dot: "bg-gray-400", label: "Unreported" },
  unavailable: { dot: "bg-gray-300", label: "Health unavailable" },
};

/** Device health (SF-019); without telemetry nothing is claimed. */
export function StatusDot({ health }: { health?: DeviceHealth }) {
  const style = health ? STATUS_STYLE[health.status] : undefined;
  const title = health ? [style!.label, ...health.reasons].join("\n") : "Health not reported";
  return <span title={title} className={`inline-block w-2 h-2 rounded-full shrink-0 ${style?.dot ?? "bg-gray-400"}`} />;
}

export function HealthBadge({ health }: { health?: DeviceHealth }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-gray-600" title={health?.reasons.join("\n")}>
      <StatusDot health={health} />
      {health ? STATUS_STYLE[health.status].label : "Health not reported"}
    </span>
  );
}
