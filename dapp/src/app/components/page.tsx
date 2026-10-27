"use client";

import { useRef, useState } from "react";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { useQuery } from "@tanstack/react-query";
import { useFleetTransaction, type TransactionPhase } from "@/hooks/useFleetTransaction";
import { fetchCatalogs, discoverVersions, buildCreateCatalogTx } from "@/lib/catalog";
import { compareVersions, type PlatformManifest } from "@/lib/component-selector";
import { useDeploymentStorage } from "@/hooks/useDeploymentStorage";
import { PageHeader } from "@/components/ui";
import { ComponentPublisher } from "@/components/ComponentPublisher";
import { GreengrassImport } from "@/components/GreengrassImport";
import { useAccessPolicies } from "@/hooks/useAccessPolicies";
import { releaseProtection } from "@/lib/delivery";
import Link from "next/link";

const button = "rounded border border-gray-300 bg-white px-3 py-2 text-sm disabled:opacity-50";

export default function ComponentsPage() {
  const account = useCurrentAccount(), client = useCurrentClient();
  const { mutateAsync: execute } = useFleetTransaction();
  const [view, setView] = useState<"library" | "publish" | "import">("library");
  const [search, setSearch] = useState(""), [catalog, setCatalog] = useState("");
  const [importBusy, setImportBusy] = useState(false);
  const [creating, setCreating] = useState(false), creatingRef = useRef(false);
  const [message, setMessage] = useState(""), [createError, setCreateError] = useState("");
  const policies = useAccessPolicies();
  const catalogs = useQuery({ queryKey: ["catalogs", account?.address], enabled: !!account, queryFn: () => fetchCatalogs(client, account!.address) });
  const selected = catalogs.data?.some(c => c.objectId === catalog) ? catalog : catalogs.data?.[0]?.objectId ?? "";
  // A catalog is an internal publishing container, not the customer-facing library.
  const versions = useQuery({
    queryKey: ["library-versions", account?.address, catalogs.data?.map(c => c.objectId)], enabled: !!account && !!catalogs.data?.length,
    queryFn: async () => (await Promise.all(catalogs.data!.map(c => discoverVersions(client, c.objectId)))).flat()
      .sort((a, b) => a.name.localeCompare(b.name) || -compareVersions(a.version, b.version) || a.objectId.localeCompare(b.objectId)),
  });
  async function create() {
    if (creatingRef.current) return;
    creatingRef.current = true; setCreating(true); setCreateError("");
    const phases: Record<TransactionPhase, string> = { validating: "Verifying fleet network…", wallet: "Confirm publishing setup in your wallet…", confirming: "Confirming setup transaction effects…" };
    try {
      await execute({ transaction: buildCreateCatalogTx(), onProgress: p => setMessage(phases[p]) });
      const previous = new Set(catalogs.data?.map(c => c.objectId));
      const refreshed = await catalogs.refetch();
      const created = refreshed.data?.find(c => !previous.has(c.objectId));
      if (created) setCatalog(created.objectId);
      setMessage("Publisher catalog confirmed. You can now publish versions.");
    } catch (e) { setMessage(""); setCreateError(String(e)); } finally { creatingRef.current = false; setCreating(false); }
  }
  const loading = catalogs.isLoading || (!!catalogs.data?.length && versions.isLoading);
  const error = catalogs.error || versions.error;
  const all = versions.data ?? [];
  const term = search.trim().toLowerCase();
  const filtered = all.filter(v => `${v.name} ${v.version} ${v.manifests.map(m => `${m.os}/${m.arch}`).join(" ")}`.toLowerCase().includes(term));
  const names = [...new Set(filtered.map(v => v.name))];
  const refresh = () => { void catalogs.refetch(); void versions.refetch(); };
  return <div className="max-w-5xl space-y-5 [overflow-wrap:anywhere]">
    <div className="flex flex-wrap items-start justify-between gap-3"><PageHeader title="Components" subtitle="Your software library · reusable components and immutable versions" />
      {account && view === "library" && <button className="rounded bg-blue-600 px-4 py-2 text-sm text-white disabled:opacity-50" disabled={loading || !!error || creating} onClick={() => setView("publish")}>Publish version</button>}
    </div>
    <p className="text-sm text-gray-600">Publish a version to store it in your library. Deploy a published version separately to choose targets and review the rollout. A library entry does not mean software is installed or running.</p>
    {!account ? <p role="status" className="rounded border bg-white p-6">Connect your wallet to browse your software library and publish versions.</p> : <>
      {error && <div role="alert" className="rounded border border-red-200 p-4 text-sm"><p>Could not load the software library: {String(error)}</p><button className={button} onClick={refresh}>Retry library</button></div>}
      {message && <p role="status" className="text-sm">{message}</p>}{createError && <p role="alert" className="text-sm text-red-700">{createError}</p>}
      {view !== "library" && <>
        {loading ? <p role="status">Loading publishing settings…</p> : error ? null : !selected ? <section className="rounded border bg-white p-6 space-y-3"><h2 className="font-semibold">Set up publishing</h2><p className="text-sm">Your first publication needs a publisher catalog on Sui. This one-time wallet transaction creates the container for your immutable releases.</p><button className={button} disabled={creating} onClick={create}>{creating ? "Setting up…" : "Set up publishing"}</button> <button className={button} disabled={creating} onClick={() => setView("library")}>Back to library</button></section> : <div key={`${account.address}/${selected}/${view}`} className="space-y-4">
          {view === "publish" ? <ComponentPublisher catalog={selected} versions={all} onClose={() => setView("library")} onPublished={refresh} /> : <><button className={button} disabled={importBusy} onClick={() => setView("library")}>Back to library</button><GreengrassImport onBusyChange={setImportBusy} catalog={selected} owner={account.address} execute={execute} onPublished={refresh} disabled={false} policies={policies.data ?? []} protect={d => releaseProtection(client, d, policies.data ?? [])} /></>}
        </div>}
      </>}
      {view === "library" && <>
        <label className="block text-sm font-medium">Search components and versions<input type="search" className="mt-1 block w-full rounded border border-gray-300 bg-white p-3 font-normal" placeholder="Name, version or platform" value={search} onChange={e => setSearch(e.target.value)} /></label>
        {loading ? <p role="status" className="rounded border bg-white p-6">Loading software library…</p> : error ? null : all.length === 0 ? <div className="rounded border bg-white p-6 space-y-2"><h2 className="font-semibold">No component versions yet</h2><p className="text-sm text-gray-600">Publish your first version with a name, version number and platform artifact. You can deploy it to devices later.</p><button className={button} disabled={creating} onClick={() => setView("publish")}>Publish your first version</button></div> : names.length === 0 ? <p role="status">No versions match “{search}”. Try another name, version or platform.</p> : <>
          <p role="status" className="text-sm text-gray-500">{names.length} component{names.length === 1 ? "" : "s"} · {filtered.length} version{filtered.length === 1 ? "" : "s"}</p>
          {names.map(name => <section key={name} aria-label={name} className="rounded border bg-white overflow-hidden"><h2 className="border-b bg-gray-50 px-4 py-3 font-semibold break-words">{name}</h2><div className="divide-y">{filtered.filter(v => v.name === name).map(v => <article key={v.objectId} className="p-4 space-y-3 min-w-0">
            <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-medium min-w-0">Version {v.version}</h3><Link href={`/firmware?componentVersion=${v.objectId}`} className="rounded border border-blue-200 px-3 py-2 text-sm text-blue-700">Deploy this version</Link></div>
            <ol aria-label="Platforms in selection order" className="space-y-2">{v.manifests.map((m, i) => <li key={i}><PlatformAvailability manifest={m} index={i} /></li>)}</ol>
            <details className="text-xs text-gray-500"><summary className="cursor-pointer">Manifest and publisher details</summary><div className="mt-2 space-y-2 break-all"><p>Version object: {v.objectId}</p><p>Publisher: {v.publisher}</p><p>Catalog: {v.catalogId}</p>{v.manifests.map((m, i) => <div key={i}><p>{i + 1}. Recipe: {m.recipeBlobId}</p><p>Recipe SHA-256: {m.recipeSha256}</p><p>Artifact SHA-256: {m.artifactSha256}</p></div>)}</div></details>
          </article>)}</div></section>)}
          <p className="text-xs text-gray-500">Platform order matters: each device selects the first compatible entry. Storage checks show current reachability, not installation, execution or future retention.</p>
        </>}
        {!loading && !error && <details className="rounded border bg-white p-4 text-sm"><summary className="cursor-pointer">Advanced publishing tools</summary><div className="space-y-3 pt-3"><label className="block">Publisher catalog<select className="mt-1 block w-full rounded border p-2" value={selected} disabled={creating} onChange={e => setCatalog(e.target.value)}>{catalogs.data?.map(c => <option key={c.objectId} value={c.objectId}>{c.objectId}</option>)}</select></label><p className="text-xs text-gray-500">All your catalogs appear in the library. This selection chooses where to publish new versions.</p><div className="flex flex-wrap gap-2"><button className={button} disabled={creating} onClick={create}>{creating ? "Creating catalog…" : "Create another publisher catalog"}</button><button className={button} disabled={creating} onClick={() => setView("import")}>Import Greengrass recipe</button></div></div></details>}
      </>}
    </>}
  </div>;
}

function PlatformAvailability({ manifest, index }: { manifest: PlatformManifest; index: number }) {
  const { data, isLoading, error } = useDeploymentStorage(manifest.recipeBlobId);
  const confidentiality = data?.confidential ? "Confidential · Seal-encrypted recipe" : data?.confidential === false ? "Public · unencrypted recipe" : "Confidentiality unknown";
  return <div className="rounded bg-gray-50 p-3 text-sm space-y-1"><p className="font-medium">{index + 1}. {manifest.os}/{manifest.arch}</p><p>{isLoading ? "Checking confidentiality and storage…" : error ? "Storage and confidentiality unknown" : confidentiality}</p>{!isLoading && <p className="text-xs text-gray-600">Recipe storage: {data?.recipe ?? "unknown"} · Artifact storage: {data?.confidential ? "private · requires authorized decryption to check" : data?.artifact ?? "unknown"}</p>}</div>;
}
