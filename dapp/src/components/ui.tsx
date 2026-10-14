import Link from "next/link";

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

export function StatusDot({ healthy }: { healthy?: boolean }) {
  return (
    <span title={healthy === undefined ? "Health not reported" : healthy ? "Healthy" : "Unhealthy"} className={`inline-block w-2 h-2 rounded-full ${healthy ? "bg-green-500" : "bg-gray-400"}`} />
  );
}
