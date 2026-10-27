"use client";
import { PublisherFailure } from "./PublisherFailure";
import { PublisherError } from "@/lib/walrus";

import { useEffect, useRef, useState } from "react";
import { useCurrentAccount, useCurrentClient } from "@mysten/dapp-kit-react";
import { useFleetTransaction, type TransactionPhase } from "@/hooks/useFleetTransaction";
import { buildPublishVersionTx, type ComponentVersion } from "@/lib/catalog";
import { validateComponent } from "@/lib/component-selector";
import { uploadArtifact, uploadRecipe, storageWarnings, renewalCommand, type UploadProgress as Progress } from "@/lib/walrus";
import { DEFAULT_DELIVERY, releaseProtection } from "@/lib/delivery";
import { useAccessPolicies } from "@/hooks/useAccessPolicies";
import { useFleetBlobStore } from "@/hooks/useFleetStorage";
import { DeliveryFields } from "./DeliveryFields";
import { FleetStorageChoice } from "./FleetStorageChoice";
import { ConfigurationFields } from "./ConfigurationFields";
import { UploadProgress } from "./UploadProgress";
import { EMPTY_CONFIGURATION, recipeConfiguration } from "@/lib/recipe-configuration";
import { assertComponentRecipe, type ComponentRecipe } from "@/lib/recipe-schema";

type Row = { id: number; os: string; arch: string; file?: File; install: string };
const input = "block w-full rounded border border-gray-300 bg-white p-2 text-sm";
const button = "rounded border border-gray-300 px-3 py-2 text-sm disabled:opacity-50";

export function ComponentPublisher({ catalog, versions, onPublished, onClose }: {
  catalog: string; versions: ComponentVersion[]; onPublished: () => void; onClose: () => void;
}) {
  const account = useCurrentAccount(), client = useCurrentClient();
  const currentAddress = useRef(account?.address); currentAddress.current = account?.address;
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const { mutateAsync: execute } = useFleetTransaction();
  const policies = useAccessPolicies();
  const [name, setName] = useState(""), [version, setVersion] = useState("");
  const [step, setStep] = useState(1);
  const nextId = useRef(1), inFlight = useRef(false);
  const [rows, setRows] = useState<Row[]>([{ id: 0, os: "linux", arch: "x86_64", install: "" }]);
  const [configuration, setConfiguration] = useState(EMPTY_CONFIGURATION);
  const [delivery, setDelivery] = useState(DEFAULT_DELIVERY);
  const fleet = useFleetBlobStore();
  const [paidChoice, setFleetPaid] = useState<boolean>();
  const fleetPaid = paidChoice ?? (fleet.configured ?? !!fleet.store);
  const [publisherFailure, setPublisherFailure] = useState<PublisherError>();
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [phase, setPhase] = useState(""), [progress, setProgress] = useState<Progress | null>(null);
  const [receipt, setReceipt] = useState<{ digest: string; warnings: string[] } | null>(null);
  const stepHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { stepHeading.current?.focus(); }, [step, receipt]);
  function change(id: number, patch: Partial<Row>) { setRows(rs => rs.map(r => r.id === id ? { ...r, ...patch } : r)); }
  function validateIdentity() {
    // Validate with the same contract-facing validator used for registration.
    validateComponent(name, version, rows.map(r => ({ os: r.os, arch: r.arch, recipeBlobId: "pending", recipeSha256: "0".repeat(64), artifactSha256: "0".repeat(64) })));
    if (versions.some(v => v.catalogId === catalog && v.name === name && v.version === version)) throw new Error("This immutable version already exists. Choose a new version number.");
  }
  function continueTo(next: number) {
    setError(""); setPublisherFailure(undefined);
    try {
      validateIdentity();
      if (next === 3 && rows.some(r => !r.file)) throw new Error("Choose an artifact for every platform.");
      setStep(next);
    } catch (e) { setError(String(e)); }
  }
  async function publish() {
    if (inFlight.current || receipt) return;
    inFlight.current = true; setBusy(true); setError(""); setPublisherFailure(undefined);
    const owner = account?.address;
    try {
      if (!owner || !catalog) throw new Error("Connect your publishing wallet and choose a catalog.");
      validateIdentity();
      if (rows.some(r => !r.file)) throw new Error("Choose an artifact for every platform.");
      const configured = recipeConfiguration(configuration);
      const recipeFor = (row: Row, artifactBlobId: string, artifactSha256: string): ComponentRecipe => ({
        schemaVersion: configured.schemaVersion, componentName: name, componentVersion: version, artifactBlobId, artifactSha256,
        lifecycle: { ...(row.install ? { install: row.install } : {}), ...(configured.validate ? { validate: configured.validate } : {}) }, configuration: configured.configuration,
      });
      for (const row of rows) assertComponentRecipe(recipeFor(row, "pending", "0".repeat(64)));
      const protection = releaseProtection(client, delivery, policies.data ?? []);
      // Fleet-paid storage is never silently replaced by the publisher.
      if (fleetPaid && !fleet.store) throw new Error(`Fleet storage is unavailable: ${fleet.reason ?? "unknown reason"}`);
      const store = fleetPaid ? fleet.store : undefined;
      const manifests = [], warnings: string[] = [];
      for (const [i, row] of rows.entries()) {
        if (!active.current || currentAddress.current !== owner) throw new Error("Wallet account changed. Reconnect the publishing account before retrying.");
        setPhase(`Uploading artifact ${i + 1} of ${rows.length} · ${row.os}/${row.arch}`); setProgress(null);
        const artifact = await uploadArtifact(row.file!, owner, protection, setProgress, store);
        setPhase(`Uploading recipe ${i + 1} of ${rows.length} · ${row.os}/${row.arch}`); setProgress(null);
        const recipe = await uploadRecipe(recipeFor(row, artifact.blobId, artifact.sha256), owner, protection, setProgress, store);
        for (const [label, storage] of [["Artifact", artifact.storage], ["Recipe", recipe.storage]] as const) {
          warnings.push(`${row.os}/${row.arch} ${label}: storage expires at epoch ${storage.endEpoch ?? "unknown"}`, ...storageWarnings(storage));
          if (storage.publisherUrl) warnings.push(`${label} ${storage.blobId} served by ${storage.publisherUrl}`);
          const renewal = renewalCommand(storage); if (renewal) warnings.push(renewal);
        }
        manifests.push({ os: row.os, arch: row.arch, recipeBlobId: recipe.blobId, recipeSha256: recipe.sha256, artifactSha256: artifact.sha256 });
      }
      if (!active.current || currentAddress.current !== owner) throw new Error("Wallet account changed. Nothing was registered by this page; uploaded blobs may remain on Walrus.");
      setProgress(null);
      const phases: Record<TransactionPhase, string> = { validating: "Verifying fleet network and publishing account…", wallet: "Confirm version publication in your wallet…", confirming: "Confirming transaction effects with the fleet node…" };
      const result = await execute({ transaction: buildPublishVersionTx(catalog, name, version, manifests), onProgress: p => setPhase(phases[p]) });
      setReceipt({ digest: result.digest, warnings }); setPhase(""); onPublished();
    } catch (e) {
      setPublisherFailure(e instanceof PublisherError ? e : undefined);
      setError(`${String(e)} Publication was not confirmed. Any completed uploads remain on Walrus; check the library before retrying registration.`);
    } finally { inFlight.current = false; setBusy(false); setProgress(null); setPhase(""); }
  }
  return <section aria-labelledby="publish-heading" className="rounded border bg-white p-4 space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 id="publish-heading" className="text-lg font-semibold">Publish version</h2><button className={button} disabled={busy} onClick={onClose}>Back to library</button></div>
    <p className="text-sm text-gray-600">Publishing adds a reusable, immutable release to your library. It does not deploy, install or start software on any device.</p>
    <ol aria-label="Publishing steps" className="flex flex-wrap gap-3 text-sm">{["Name & version", "Platform artifacts", "Review & publish"].map((s, i) => <li key={s} aria-current={step === i + 1 ? "step" : undefined} className={step === i + 1 ? "font-semibold text-blue-700" : "text-gray-500"}>{i + 1}. {s}</li>)}</ol>
    {error && <p role="alert" className="text-sm text-red-700 break-words">{error}</p>}
    {publisherFailure && <PublisherFailure error={publisherFailure} />}
    {receipt ? <div role="status" className="space-y-2 break-words"><h3 ref={stepHeading} tabIndex={-1} className="font-semibold">Version published</h3><p>{name} {version} is in the library. Choose “Deploy this version” there to select targets and review a deployment.</p><p className="text-xs">Confirmed transaction: {receipt.digest}</p><details><summary>Storage expiry and renewal</summary><pre className="whitespace-pre-wrap text-xs">{receipt.warnings.join("\n")}</pre></details></div> : <>
      <h3 ref={stepHeading} tabIndex={-1} className="font-semibold">{["Name & version", "Platform artifacts", "Review & publish"][step - 1]}</h3>
      <fieldset disabled={busy} className="space-y-4 min-w-0">
        {step === 1 && <><label className="block text-sm">Component name<input className={input} placeholder="my-app" value={name} onChange={e => setName(e.target.value)} /></label><label className="block text-sm">Semantic version<input className={input} placeholder="1.0.0" value={version} onChange={e => setVersion(e.target.value)} /></label><p className="text-sm text-gray-500">Use a stable name for your software. Published versions cannot be overwritten; publish a new version for changes.</p><button className={button} onClick={() => continueTo(2)}>Continue to platform artifacts</button></>}
        {step === 2 && <>
          <p className="text-sm">Add your first platform artifact, then any additional platforms. Devices use the first compatible platform in this order; put wildcard fallbacks last.</p>
          {rows.map((r, i) => <fieldset key={r.id} className="rounded border p-3 space-y-3 min-w-0"><legend className="px-1 text-sm font-semibold">Platform {i + 1}</legend><div className="grid gap-3 sm:grid-cols-2"><label className="text-sm">Operating system<select className={input} value={r.os} onChange={e => change(r.id, { os: e.target.value })}>{["linux", "macos", "*"].map(os => <option key={os} value={os}>{os === "*" ? "Any OS (*)" : os}</option>)}</select></label><label className="text-sm">Architecture<select className={input} value={r.arch} onChange={e => change(r.id, { arch: e.target.value })}>{["x86_64", "aarch64", "arm", "x86", "*"].map(a => <option key={a} value={a}>{a === "*" ? "Any architecture (*)" : a}</option>)}</select></label></div>
            <label className="block text-sm">Artifact<input type="file" className={input} onChange={e => change(r.id, { file: e.target.files?.[0] })} /></label>
            <details><summary className="cursor-pointer text-sm">Optional installer command</summary><label className="block text-sm mt-2">Install command<input className={input} value={r.install} onChange={e => change(r.id, { install: e.target.value })} /></label><p className="text-xs text-gray-500">Use an explicitly configured platform tool. Your platform owns OS installation, reboot and firmware recovery; SuiFleet assumes no service manager or image format.</p></details>
            <div className="flex flex-wrap gap-2">{rows.length > 1 && <button className={button} onClick={() => setRows(rs => rs.filter(row => row.id !== r.id))}>Remove platform {i + 1}</button>}{i > 0 && <button className={button} onClick={() => setRows(rs => { const index = rs.findIndex(row => row.id === r.id); if (index < 1) return rs; const copy = [...rs]; [copy[index - 1], copy[index]] = [copy[index], copy[index - 1]]; return copy; })}>Move platform {i + 1} earlier</button>}</div>
          </fieldset>)}
          <button className={button} disabled={rows.length >= 16} onClick={() => setRows(rs => [...rs, { id: nextId.current++, os: "linux", arch: "aarch64", install: "" }])}>Add platform</button>
          <details><summary className="cursor-pointer text-sm">Optional configuration and validation</summary><ConfigurationFields value={configuration} onChange={setConfiguration} disabled={busy} /></details>
          <DeliveryFields value={delivery} onChange={setDelivery} policies={policies.data ?? []} disabled={busy} />
          <FleetStorageChoice checked={fleetPaid} onChange={setFleetPaid} available={!!fleet.store} storage={fleet.storage} reason={fleet.reason} retention={fleet.retention} disabled={busy} />
          {policies.error && <p role="alert">Access policies could not be loaded: {String(policies.error)}</p>}
          <div className="flex gap-2"><button className={button} onClick={() => setStep(1)}>Back to name & version</button><button className={button} onClick={() => continueTo(3)}>Review version</button></div>
        </>}
        {step === 3 && <><h3 className="font-semibold">{name} · {version}</h3><p className="text-sm">{delivery.mode === "confidential" ? "Confidential · Seal encrypted before upload" : "Public · unencrypted"}</p><ol className="space-y-1 text-sm">{rows.map((r, i) => <li key={r.id} className="break-words">{i + 1}. {r.os}/{r.arch} · {r.file?.name}{r.install && ` · install: ${r.install}`}</li>)}</ol><p className="text-sm">Artifacts and recipes will upload first. Then confirm registration in your wallet. Success requires confirmed execution effects. No devices are targeted.</p><div className="flex flex-wrap gap-2"><button className={button} onClick={() => setStep(2)}>Edit platform artifacts</button><button className="rounded bg-blue-600 px-4 py-2 text-sm text-white disabled:opacity-50" disabled={!catalog || !account || busy} onClick={publish}>{busy ? "Publishing…" : "Upload and publish version"}</button></div></>}
      </fieldset>
      {busy && <div className="space-y-2"><p role="status" className="text-sm">{phase}</p>{progress && <UploadProgress progress={progress} label={phase} />}</div>}
    </>}
  </section>;
}
