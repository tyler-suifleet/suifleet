import Link from "next/link";
import { describeServices, type DeviceHealth } from "@/lib/telemetry";

export function TelemetrySettings({ configured }: { configured: boolean }) {
  return <p className="text-xs text-gray-500">{configured ? "Health comes from verified device reports." : "Device health reporting is not set up for this fleet. Ask your administrator to finish the setup."} <Link href="/data" className="text-blue-700 underline">Read encrypted logs and diagnostics.</Link></p>;
}

export function HealthDetails({ health }: { health?: DeviceHealth }) {
  if (!health) return <p className="text-xs text-gray-400">Loading health…</p>;
  return (
    <div className="space-y-2 text-xs">
      {health.reasons.length > 0 && (
        <ul className="list-disc pl-4 text-amber-800">
          {health.reasons.map((reason, i) => <li key={i}>{reason}</li>)}
        </ul>
      )}
      {health.lastSeenMs !== undefined && (
        <p className="text-gray-500">
          Last report {new Date(health.lastSeenMs).toLocaleString()} (taken {new Date(health.observedMs!).toLocaleString()} by the device clock),
          client {health.daemonVersion}. Reports are checked against the registered device identity.
        </p>
      )}
      {health.services && (
        <div>
          <p className="font-medium text-gray-600">Services</p>
          <ul className="text-gray-600 whitespace-pre-wrap">
            {describeServices(health.services).map((line, i) => <li key={i}>{line}</li>)}
          </ul>
        </div>
      )}
      {health.components.length === 0 ? (
        <p className="text-gray-400">No installed components reported.</p>
      ) : (
        <table className="w-full">
          <thead>
            <tr className="text-left text-gray-500">
              {["Component", "Version", "Installed", "Runtime", "Restarts", "Last exit"].map((h) => <th key={h} className="py-1 pr-4 font-medium">{h}</th>)}
            </tr>
          </thead>
          <tbody>
            {health.components.map((c) => (
              <tr key={c.name}>
                <td className="py-1 pr-4 font-medium">{c.name}</td>
                <td className="py-1 pr-4">{c.version}</td>
                <td className="py-1 pr-4">{c.installed ? "yes" : "not confirmed"}</td>
                <td className="py-1 pr-4">{c.supervised ? c.phase ?? "not started" : "one-shot"}</td>
                <td className="py-1 pr-4">{c.restarts ?? "—"}</td>
                <td className="py-1 pr-4 text-gray-500">{c.last_exit ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
