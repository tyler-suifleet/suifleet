"use client";

import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useFleetDataConfig } from "@/hooks/useFleetDataConfig";
// Per-device services and alert drill-down (SF-084), read only through the
// SF-082 verified chain path. See lib/device-services.ts for the rules.
import { useEffect, useState } from "react";
import Link from "next/link";
import { useCurrentAccount, useCurrentClient } from "@mysten/dapp-kit-react";
import { normalizeSuiAddress as id } from "@mysten/sui/utils";
import { useDevices } from "@/hooks/useDevices";
import { useFleetHealth } from "@/hooks/useFleetHealth";
import { useFleetAlerts } from "@/hooks/useFleetAlerts";
import { listFleetDeviceIds } from "@/lib/fleet-data";
import { deviceAlerts, deviceHref, deviceServicesView, linkedDevice, utc, type ServicesView, type Unavailable } from "@/lib/device-services";
import type { ShadowSyncHealth } from "@/lib/telemetry";
import { HealthBadge, PageHeader, Section } from "@/components/ui";

export default function DeviceServicesPage() {
  const DATA_CONFIG = useFleetDataConfig();
  const selection = useFleetSelection();
  const client = useCurrentClient(), account = useCurrentAccount();
  const { data: owned = [] } = useDevices();
  const [devices, setDevices] = useState<string[]>([]), [cursor, setCursor] = useState<string | null>(null), [more, setMore] = useState(false);
  const [device, setDevice] = useState(""), [indexError, setIndexError] = useState("");
  useEffect(() => {
    const select = () => { const linked = linkedDevice(new URLSearchParams(window.location.search).get("device")); if (linked) setDevice(linked); };
    select(); window.addEventListener("popstate", select);
    return () => window.removeEventListener("popstate", select);
  }, []);
  useEffect(() => {
    let live = true; setDevices([]); setCursor(null); setMore(false); setIndexError("");
    if (DATA_CONFIG && account) void listFleetDeviceIds(client, DATA_CONFIG).then((page) => {
      if (live) { setDevices(page.devices); setCursor(page.cursor); setMore(page.hasNextPage); }
    }).catch(() => { if (live) setIndexError("Fleet device index unavailable."); });
    return () => { live = false; };
  }, [account?.address, client]);
  async function loadMore() {
    if (!DATA_CONFIG) return;
    try {
      const page = await listFleetDeviceIds(client, DATA_CONFIG, cursor);
      setDevices((old) => [...new Set([...old, ...page.devices])]); setCursor(page.cursor); setMore(page.hasNextPage);
    } catch { setIndexError("Fleet device index page unavailable."); }
  }
  const visible = selection.supported ? devices.filter(d => selection.fleet?.devices.includes(d)) : devices;
  const selectedDevice = selection.supported && !selection.fleet?.devices.includes(device) ? "" : device;
  const name = (device: string) => owned.find((d) => id(d.objectId) === id(device))?.name ?? device;
  return <div className="max-w-5xl space-y-4">
    <PageHeader title="Device services" subtitle="Shadows, streams, messaging, bridge and alerts from each device's signed, chain-anchored report" />
    {!DATA_CONFIG ? <p>Direct-chain fleet data is not configured for this deployment; device service views need the verified Sui path.</p>
      : !account ? <p>Connect a wallet to read this fleet.</p> : <>
      <label>Device <select className="border rounded p-2" value={selectedDevice} onChange={(e) => { setDevice(e.target.value); window.history.replaceState(null, "", e.target.value ? `?${new URLSearchParams({ device: e.target.value })}` : "?"); }}>
        <option value="">Choose a device</option>
        {[...new Set([...(selectedDevice ? [selectedDevice] : []), ...visible])].map((d) => <option key={d} value={d}>{name(d)}</option>)}
      </select></label>
      {more && <button className="border rounded p-2 ml-3" onClick={() => void loadMore()}>Load more devices</button>}
      {indexError && <p role="alert">{indexError}</p>}
      {selectedDevice && <DeviceDetail key={selectedDevice} device={selectedDevice} name={name(selectedDevice)} indexed={visible.includes(selectedDevice) || !more} />}
    </>}
  </div>;
}

function DeviceDetail({ device, name, indexed }: { device: string; name: string; indexed: boolean }) {
  const { data: health = {} } = useFleetHealth([device]);
  const alerts = deviceAlerts(useFleetAlerts(), device);
  const view = deviceServicesView(health[device]);
  return <div className="space-y-4" data-testid="device-services">
    <p className="flex items-center gap-3"><span className="font-medium">{name}</span><span className="font-mono text-xs">{device}</span><HealthBadge health={health[device]} />
      <Link className="text-[#4DA2FF] text-sm hover:underline" href={deviceHref("/data/", device)}>Logs and diagnostics</Link></p>
    {!indexed && <p className="text-sm">This device is not in the loaded part of the fleet index.</p>}
    {view.state === "unavailable" ? <Missing what="Service health" value={view} /> : <Services view={view} device={device} />}
    <Section title="Recent alerts">
      {alerts.state === "unavailable" ? <Missing what="Alerts" value={alerts} />
        : alerts.items.length === 0 ? <p className="text-sm">No active alerts for this device.</p>
        : <ul className="divide-y text-sm" data-testid="device-alerts">{alerts.items.map((a) => <li key={a.key} className="py-2">
            <span className={`mr-2 rounded px-1.5 py-0.5 text-xs font-medium ${a.severity === "critical" ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700"}`}>{a.severity}</span>
            <span className="font-medium">{a.kind}</span> — {a.message}
            <p className="text-xs text-gray-500">{a.action}{a.sinceMs !== undefined ? ` · since ${utc(a.sinceMs)}` : ""}{a.component ? ` · component ${a.component}` : ""}
              {a.deploymentId && <> · <Link className="text-[#4DA2FF] hover:underline" href="/deployments">deployment {a.deploymentId}</Link></>}</p>
          </li>)}</ul>}
    </Section>
  </div>;
}

function Missing({ what, value }: { what: string; value: Unavailable }) {
  return <p role="status" className="text-sm text-gray-600"><span className="font-medium">{what} unavailable.</span> {value.reason}</p>;
}

function Services({ view, device }: { view: ServicesView; device: string }) {
  const { messaging, bridge, streams, shadows } = view;
  const off = <p className="text-sm text-gray-500">Not enabled on this device.</p>;
  const more = (omitted: number) => omitted > 0 && <p className="text-xs text-gray-500">{omitted} more not listed (snapshot size limit).</p>;
  return <>
    <p className="text-xs text-gray-500">Signed report observed {utc(view.observedMs)}, anchored on chain {utc(view.receivedMs)}.</p>
    <Section title="Shadows">
      {shadows.state === "disabled" ? off : <div className="space-y-2 text-sm" data-testid="shadows">
        <p>{shadows.shadows} shadows; {shadows.notificationsRefused} delta notifications refused ({shadows.notificationsRefusedRecent} since the previous report).</p>
        <p className="text-gray-600" data-testid="shadow-documents-link">{shadows.documents.reason} <Link className="text-[#4DA2FF] hover:underline" href={deviceHref("/data/", device)}>Open shadow documents</Link></p>
        {shadows.sync.length > 0 && <table className="text-left"><thead><tr><th className="pr-4">Synchronized shadow</th><th className="pr-4">Conflicts (recent)</th><th className="pr-4">Consecutive failures</th><th className="pr-4">Last success</th><th>Last error</th></tr></thead>
          <tbody>{shadowRows(shadows.sync)}</tbody></table>}
        {more(shadows.omitted)}
      </div>}
    </Section>
    <Section title="Streams">
      {streams.state === "disabled" ? off : <div className="space-y-2 text-sm" data-testid="streams">
        {streams.streams.length === 0 && <p>No streams.</p>}
        {streams.streams.map((s) => <div key={s.name} className="border-b pb-2">
          <p className="font-medium">{s.name}</p>
          <p>{s.bytes} of {s.max_size} bytes ({s.fill_percent}%), {s.messages} messages, {s.overwrite ? "overwrites oldest when full" : "refuses appends when full"}; {s.rejected} appends rejected ({s.rejected_recent} recent).</p>
          {s.positions.state === "unavailable" ? <p className="text-gray-600">Oldest/newest sequence: unavailable. {s.positions.reason}</p>
            : <p data-testid="stream-positions">{s.positions.oldest === null ? "No messages retained." : `Oldest sequence ${s.positions.oldest}, newest ${s.positions.newest}.`}</p>}
          {s.exportState.state === "no-export" ? <p>No export configured.</p> : <p>
            Export checkpoint: {s.exportState.checkpoint.state === "unavailable" ? "unavailable (services version 1)" : s.exportState.checkpoint.checkpoint === null ? "nothing exported yet" : `sequence ${s.exportState.checkpoint.checkpoint}`};
            {" "}{s.exportState.health.lag} messages behind; {s.exportState.health.batches} batches exported; {s.exportState.health.lost} lost before export ({s.exportState.health.lost_recent} recent);
            {" "}{s.exportState.health.failures} failures, {s.exportState.health.consecutive_failures} consecutive; last success {utc(s.exportState.health.last_success_ms)}{s.exportState.health.last_error ? `; last error ${s.exportState.health.last_error}` : ""}.</p>}
        </div>)}
        {more(streams.omitted)}
      </div>}
    </Section>
    <Section title="Messaging and bridge">
      <div className="space-y-2 text-sm" data-testid="messaging">
        {messaging.state === "disabled" ? <p className="text-gray-500">Pub/sub not enabled.</p> : <>
          <p>Pub/sub: {messaging.subscriptions} subscriptions, {messaging.queued} queued, {messaging.refused} refused, {messaging.dropped} dropped.</p>
          {messaging.queues.map((q) => <p key={q.principal} className="ml-4">{q.principal}: {q.queued} queued ({q.fill_percent}% full{q.full_since_ms !== null ? ` since ${utc(q.full_since_ms)}` : ""}); {q.refused_recent} refused and {q.dropped_recent} dropped since the previous report.</p>)}
          {more(messaging.omitted)}
        </>}
        {bridge.state === "disabled" ? <p className="text-gray-500">MQTT bridge not enabled.</p>
          : <p>MQTT bridge: {bridge.connected ? "connected" : "disconnected"} since {utc(bridge.since_ms)}; {bridge.connects} connects, {bridge.refused} refused ({bridge.refused_recent} recent), {bridge.rejected} rejected ({bridge.rejected_recent} recent){bridge.last_error ? `; last error ${bridge.last_error}` : ""}.</p>}
      </div>
    </Section>
  </>;
}

function shadowRows(sync: ShadowSyncHealth[]) {
  return sync.map((s) => <tr key={s.name}><td className="pr-4 font-mono">{s.name}</td><td className="pr-4">{s.conflicts} ({s.conflicts_recent})</td>
    <td className="pr-4">{s.consecutive_failures}</td><td className="pr-4">{utc(s.last_success_ms)}</td><td>{s.last_error ?? "none"}</td></tr>);
}
