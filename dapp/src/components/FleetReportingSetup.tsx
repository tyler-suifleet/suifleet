"use client";
import { useEffect, useRef, useState } from "react";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useDevices } from "@/hooks/useDevices";
import { useFleetReporting } from "@/hooks/useFleetDataConfig";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { buildEnrollReporting, buildProvisionReporting, reportingClientSettings, reportingRelease } from "@/lib/fleet-reporting";

function reportingFailure(value: string) {
  if (/User rejected/i.test(value)) return "You declined account approval. Refresh reporting status before retrying.";
  if (/Enroll device|network account/i.test(value)) return "Enable reporting for this device with its current network account before downloading settings.";
  if (/administrator|read-only|revoked/i.test(value)) return "A current fleet administrator must approve this action.";
  return "The action could not be verified. Refresh reporting status before retrying; a network error can hide a confirmed change. Your organization’s approval requirements still apply.";
}

export function FleetReportingSetup() {
  const { fleet } = useFleetSelection(), client = useCurrentClient(), query = useFleetReporting(), mutation = useFleetTransaction(), devices = useDevices();
  const [device, setDevice] = useState(""), [message, setMessage] = useState(""), [error, setError] = useState(""), [confirmation, setConfirmation] = useState("");
  const guard = useRef(false), mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  if (!fleet) return null;
  let releaseReady = false, releaseError = "";
  try { releaseReady = !!reportingRelease(); } catch { releaseError = "Reporting setup configuration is invalid; readiness could not be verified."; }
  const selected = fleet.devices.includes(device) ? device : fleet.devices[0] ?? "";
  async function run(action: "setup" | "enroll" | "download") {
    if (!fleet || guard.current) return;
    guard.current = true; setError(""); setConfirmation(""); setMessage("Verifying current fleet configuration…");
    try {
      if (action === "download") {
        const text = await reportingClientSettings(client, fleet, selected);
        if (!mounted.current) return;
        const url = URL.createObjectURL(new Blob([text], { type: "application/toml" }));
        const link = document.createElement("a"); link.href = url; link.download = "fleet-reporting.toml";
        document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        setMessage("Downloaded verified settings. Merge them into the device client’s existing configuration, keeping its key and saved history. The device remains unreported until its client sends a verified health report.");
      } else {
        const transaction = action === "setup" ? await buildProvisionReporting(client, fleet) : await buildEnrollReporting(client, fleet, query.data!, selected);
        if (!mounted.current) return;
        const result = await mutation.mutateAsync({ transaction });
        const refreshed = await query.refetch();
        if (refreshed.error || !refreshed.data) throw refreshed.error ?? Error("Confirmed transaction; reporting binding is not yet verified. Refresh before retrying.");
        setConfirmation(result.digest);
        setMessage("Reporting settings verified. Enable device reporting, download its settings and install them on the device.");
      }
    } catch (cause) {
      setMessage(""); setError(String(cause));
      await query.refetch();
    } finally { guard.current = false; }
  }
  return <section className="space-y-3 rounded-xl border border-slate-200 bg-white p-5" aria-label="Reporting setup">
    <h3 className="font-semibold">Set up reporting and storage</h3>
    <p>Each fleet has its own reporting and storage settings. Your administrator account approves setup once, and your wallet shows the network fee before approval. Storage starts empty; add storage funds in Network budget before uploading logs.</p>
    <p role="status">{query.isPending ? "Pending: checking reporting readiness…" : query.isError ? "Failed: readiness could not be verified." : query.data ? "Verified: this fleet’s reporting and storage settings." : "Not set up: this fleet has no verified reporting configuration."}</p>
    {query.error && <p role="alert">We could not verify reporting settings. Refresh to try again.</p>}
    <button type="button" onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh readiness</button>
    {!query.data && <button type="button" disabled={!fleet.administrator || query.isPending || query.isError || mutation.isPending || !releaseReady} onClick={() => void run("setup")}>Set up fleet reporting</button>}
    {releaseError && <p role="alert">{releaseError}</p>}
    {!releaseReady && <p>Reporting setup is not available on this network yet. Existing reporting stays available only after verification.</p>}
    {!fleet.administrator && <p>A current fleet administrator must approve setup and device enrollment.</p>}
    {query.data && <div className="space-y-2">
      <label>Device <select value={selected} onChange={e => setDevice(e.target.value)}>{fleet.devices.map((id, index) => <option key={id} value={id}>{devices.data?.find(d => d.objectId === id)?.name || `Device ${index + 1}`}</option>)}</select></label>
      <button type="button" disabled={!selected || !fleet.administrator || mutation.isPending || query.isError || query.isFetching} onClick={() => void run("enroll")}>Enable device reporting</button>
      <button type="button" disabled={!selected || mutation.isPending || query.isError || query.isFetching} onClick={() => void run("download")}>Download device reporting settings</button>
      <p>Install the matching network settings and use the device’s current identity and network account. Verified settings do not mean the device has reported.</p>
    </div>}
    {message && <p role="status">{message}</p>}{error && <p role="alert">{reportingFailure(error)}</p>}
    <details><summary>Technical details</summary>
      {selected && <p>Device ID: <code>{selected}</code></p>}
      {confirmation && <p>Confirmed transaction: <code>{confirmation}</code></p>}
      <p>Merge fleet-reporting.toml into /etc/suifleet/config.toml, retaining existing keys, journals and tuned budget settings. Install the matching deployment.json.</p>
      {(error || query.error) && <pre className="whitespace-pre-wrap break-words">{error || query.error?.message}</pre>}
    </details>
  </section>;
}
