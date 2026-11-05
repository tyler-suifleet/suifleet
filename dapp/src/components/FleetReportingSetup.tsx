"use client";
import { useEffect, useRef, useState } from "react";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useFleetReporting } from "@/hooks/useFleetDataConfig";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { buildEnrollReporting, buildProvisionReporting, reportingClientSettings, reportingRelease } from "@/lib/fleet-reporting";

export function FleetReportingSetup() {
  const { fleet } = useFleetSelection(), client = useCurrentClient(), query = useFleetReporting(), mutation = useFleetTransaction();
  const [device, setDevice] = useState(""), [message, setMessage] = useState(""), [error, setError] = useState("");
  const guard = useRef(false), mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  if (!fleet) return null;
  const selected = fleet.devices.includes(device) ? device : fleet.devices[0] ?? "";
  async function run(action: "setup" | "enroll" | "download") {
    if (!fleet || guard.current) return;
    guard.current = true; setError(""); setMessage("Verifying current fleet configuration…");
    try {
      if (action === "download") {
        const text = await reportingClientSettings(client, fleet, selected);
        if (!mounted.current) return;
        const url = URL.createObjectURL(new Blob([text], { type: "application/toml" }));
        const link = document.createElement("a"); link.href = url; link.download = "fleet-reporting.toml";
        document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        setMessage("Downloaded verified settings. Merge the fragment into the device configuration, preserving its keys and journals. Reporting remains unreported until the daemon submits a verified sample.");
      } else {
        const transaction = action === "setup" ? await buildProvisionReporting(client, fleet) : await buildEnrollReporting(client, fleet, query.data!, selected);
        if (!mounted.current) return;
        const result = await mutation.mutateAsync({ transaction });
        const refreshed = await query.refetch();
        if (refreshed.error || !refreshed.data) throw refreshed.error ?? Error("Confirmed transaction; reporting binding is not yet verified. Refresh before retrying.");
        setMessage(`Confirmed ${result.digest}. Reporting configuration verified. Enroll the device and download its settings.`);
      }
    } catch (cause) {
      setMessage(""); setError(`${String(cause)}. Refresh readiness before retrying; a network error can hide a confirmed transaction. Organization protections remain required.`);
      await query.refetch();
    } finally { guard.current = false; }
  }
  return <section className="space-y-3 rounded-xl border border-slate-200 bg-white p-5" aria-label="Reporting setup">
    <h3 className="font-semibold">Set up reporting and storage</h3>
    <p>Each fleet has its own reporting and storage objects. Setup asks your administrator wallet to approve one atomic transaction. Storage starts empty; fund archive storage from Network budget before uploading logs.</p>
    <p role="status">{query.isPending ? "Pending: checking reporting readiness…" : query.isError ? "Failed: readiness could not be verified." : query.data ? "Verified: fleet reporting and storage bindings." : "Not set up: this fleet has no verified reporting configuration."}</p>
    {query.error && <p role="alert">{query.error.message}</p>}
    <button type="button" onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh readiness</button>
    {!query.data && <button type="button" disabled={!fleet.administrator || query.isPending || query.isError || mutation.isPending || !reportingRelease()} onClick={() => void run("setup")}>Set up fleet reporting</button>}
    {!reportingRelease() && <p>This deployment needs a reviewed reporting setup release. Existing explicit locators remain available only after verification.</p>}
    {!fleet.administrator && <p>A current fleet administrator must approve setup and device enrollment.</p>}
    {query.data && <div className="space-y-2">
      <label>Device <select value={selected} onChange={e => setDevice(e.target.value)}>{fleet.devices.map(id => <option key={id} value={id}>{id}</option>)}</select></label>
      <button type="button" disabled={!selected || !fleet.administrator || mutation.isPending || query.isError || query.isFetching} onClick={() => void run("enroll")}>Enable device reporting</button>
      <button type="button" disabled={!selected || mutation.isPending || query.isError || query.isFetching} onClick={() => void run("download")}>Download device reporting settings</button>
      <p>Install the matching deployment.json and use the device’s current identity and network account. Configuration verification does not mean the device has reported.</p>
    </div>}
    {message && <p role="status">{message}</p>}{error && <p role="alert">{error}</p>}
  </section>;
}
