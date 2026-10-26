import Link from "next/link";
import type { DeviceHealth, FleetStatus } from "@/lib/telemetry";

// ── Shared components ──────────────────────────────────────────────────────────

export function PageHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <header className="mb-6 space-y-2">
      <h1 className="text-2xl md:text-3xl font-semibold tracking-tight text-slate-900">{title}</h1>
      {subtitle && <p className="max-w-3xl text-sm leading-relaxed text-slate-600">{subtitle}</p>}
    </header>
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
    <div className="bg-white border border-slate-200 rounded-xl shadow-sm min-w-0">
      <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-4 border-b border-slate-100">
        <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
        {action && (
          <Link href={action.href} className="text-sm font-medium text-blue-700 hover:underline">
            {action.label}
          </Link>
        )}
      </div>
      <div className="px-5 py-5 overflow-x-auto">{children}</div>
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

/** Consistent disconnected states retain the page context and a clear next step. */
export function ConnectionRequired({ title, description }: { title: string; description: string }) {
  return <div className="max-w-5xl space-y-6">
    <PageHeader title={title} subtitle={description} />
    <section className="rounded-xl border border-slate-200 bg-white p-8 shadow-sm">
      <div aria-hidden="true" className="mb-4 flex h-11 w-11 items-center justify-center rounded-xl bg-blue-50 text-blue-700">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><rect x="4" y="5" width="16" height="14" rx="3"/><path d="M15 10h5v5h-5z"/></svg>
      </div>
      <h2 className="font-semibold text-slate-900">Connect your account to continue</h2>
      <p role="status" className="mt-2 max-w-lg text-sm leading-relaxed text-slate-600">Use the wallet control in the top bar to connect the account that manages your fleet.</p>
      <Link href="/get-started" className="mt-5 inline-flex text-sm font-medium text-blue-700 hover:underline">New to SuiFleet? Set up your first device →</Link>
    </section>
  </div>;
}
