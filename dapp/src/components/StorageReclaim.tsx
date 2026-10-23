"use client";

// SF-129 owner reclaim of storage deposits. Every action is a dry run first:
// the transaction is simulated with the owner's own gas and the recoverable
// SUI is shown; signing rebuilds the same plan and reports actual effects.
import { useState } from "react";
import { useCurrentAccount, useCurrentClient } from "@mysten/dapp-kit-react";
import { useQueryClient } from "@tanstack/react-query";
import type { Transaction } from "@mysten/sui/transactions";
import { PACKAGE_ID } from "@/lib/constants";
import { DATA_CONFIG, listFleetDeviceIds } from "@/lib/fleet-data";
import type { StorageConfig } from "@/lib/fleet-budget";
import type { DeploymentEvent } from "@/lib/sui";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import {
  buildArchiveReclaimTx, buildRetireDeploymentsTx, formatReclaim, netReclaim, planArchiveReclaim, planDeploymentReclaim, previewReclaim,
  type ArchivePlan, type DeploymentReclaim, type ReclaimPreview,
} from "@/lib/storage-reclaim";

function useReclaim(build: () => Transaction) {
  const account = useCurrentAccount(), client = useCurrentClient(), cache = useQueryClient(), mutation = useFleetTransaction();
  const [preview, setPreview] = useState<ReclaimPreview>(), [result, setResult] = useState<string>(), [error, setError] = useState<string>();
  async function dryRun() {
    setPreview(undefined); setResult(undefined); setError(undefined);
    try { setPreview(await previewReclaim(client, build(), account!.address)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  }
  async function sign() {
    setError(undefined);
    try {
      const tx = build();
      tx.setSender(account!.address);
      const { digest } = await mutation.mutateAsync({ transaction: tx });
      const executed = await client.waitForTransaction({ digest, include: { effects: true } });
      const gas = executed.Transaction?.effects?.gasUsed;
      setResult(gas ? `Confirmed ${digest}: ${formatReclaim(netReclaim(gas).netMist)} returned after fees.` : `Confirmed ${digest}.`);
      setPreview(undefined);
      await Promise.all([cache.invalidateQueries({ queryKey: ["fleet-storage"] }), cache.invalidateQueries({ queryKey: ["retention"] }),
        cache.invalidateQueries({ queryKey: ["deployments"] })]);
    } catch (cause) {
      setError(`${cause instanceof Error ? cause.message : "Wallet request failed"}. If it may have reached the network, refresh before retrying.`);
    }
  }
  return { dryRun, sign, preview, result, error, pending: mutation.isPending, ready: !!account };
}

function PreviewLine({ preview }: { preview: ReclaimPreview }) {
  if (!preview.ok) return <p role="alert">Dry run refused: {preview.error}. Nothing was signed; refresh the plan.</p>;
  return <p role="status">Dry run: {formatReclaim(preview.netMist)} recoverable after this transaction&apos;s own fees (rebate {formatReclaim(preview.rebateMist)},
    computation {formatReclaim(preview.computationMist)}, new storage {formatReclaim(preview.storageCostMist)}). The wallet shows the final amount; WAL already paid to Walrus is not refunded.</p>;
}

/** Expired archive anchors and blobs of this fleet's devices. */
export function ArchiveReclaimPanel({ config, capId, epoch }: { config: StorageConfig; capId: string; epoch?: number }) {
  const client = useCurrentClient();
  const [plan, setPlan] = useState<ArchivePlan>(), [scanError, setScanError] = useState<string>(), [scanning, setScanning] = useState(false);
  const reclaim = useReclaim(() => buildArchiveReclaimTx(DATA_CONFIG!, capId, config.walrus.system, plan!).tx);
  if (!DATA_CONFIG) return null;
  async function scan() {
    setScanning(true); setScanError(undefined); setPlan(undefined);
    try {
      const devices: string[] = [];
      let cursor: string | null | undefined;
      do {
        const page = await listFleetDeviceIds(client, DATA_CONFIG!, cursor);
        devices.push(...page.devices);
        cursor = page.hasNextPage ? page.cursor : undefined;
      } while (cursor);
      setPlan(await planArchiveReclaim(client, DATA_CONFIG!, config.storageOrigin ?? config.packageId, devices, epoch!));
    } catch (cause) { setScanError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setScanning(false); }
  }
  const count = plan ? plan.anchors.length + plan.loose.length : 0;
  return <fieldset className="border rounded p-2 space-y-2" disabled={scanning || reclaim.pending}>
    <legend className="px-1 font-semibold">Reclaim expired archives</legend>
    <p>Deletes the chain index entry of each archive whose Walrus storage has ended, releases its blob from fleet storage and burns it, returning those storage deposits. Archives whose storage was renewed are kept. Expired archives are no longer readable from Walrus; export anything you must keep before it expires.</p>
    <button type="button" disabled={epoch === undefined} onClick={() => void scan()}>Find expired archives</button>
    {scanError && <p role="alert">Archive scan failed: {scanError}</p>}
    {plan && <p role="status">Walrus epoch {plan.epoch}: {plan.anchors.length} expired indexed archive(s) and {plan.loose.length} unindexed expired device blob(s) of {plan.scanned} live index entries.{plan.truncated ? " More history exists past the scan bound; reclaim again afterwards." : ""}</p>}
    {plan && count > 0 && <div className="flex gap-2">
      <button type="button" onClick={() => void reclaim.dryRun()}>Dry run</button>
      <button type="button" disabled={!reclaim.preview?.ok} onClick={() => void reclaim.sign()}>Sign and reclaim</button>
    </div>}
    {reclaim.preview && <PreviewLine preview={reclaim.preview} />}
    {reclaim.result && <p role="status">{reclaim.result}</p>}
    {reclaim.error && <p role="alert">{reclaim.error}</p>}
  </fieldset>;
}

/** The account's finished deployment records that no group still desires. */
export function DeploymentReclaimPanel({ deployments }: { deployments: DeploymentEvent[] }) {
  const client = useCurrentClient(), account = useCurrentAccount();
  const [plan, setPlan] = useState<DeploymentReclaim[]>(), [scanError, setScanError] = useState<string>(), [scanning, setScanning] = useState(false);
  const reclaim = useReclaim(() => buildRetireDeploymentsTx(PACKAGE_ID, plan!.filter((r) => r.eligible).slice(0, 64)));
  if (!account) return null;
  async function scan() {
    setScanning(true); setScanError(undefined); setPlan(undefined);
    try { setPlan(await planDeploymentReclaim(client, deployments, account!.address)); }
    catch (cause) { setScanError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setScanning(false); }
  }
  const eligible = plan?.filter((r) => r.eligible) ?? [], kept = plan?.filter((r) => !r.eligible && r.reason !== "still in progress") ?? [];
  return <fieldset className="border rounded p-2 space-y-2 text-xs" disabled={scanning || reclaim.pending}>
    <legend className="px-1 font-semibold">Reclaim finished deployment records</legend>
    <p>Deletes your records that are cancelled, expired or reported by every target, after the chain confirms no target group&apos;s desired state still uses them. Catalog bindings are removed first. Devices ignore deleted records; their update history stays in chain events.</p>
    <button type="button" onClick={() => void scan()}>Find finished records</button>
    {scanError && <p role="alert">Scan failed: {scanError}</p>}
    {plan && <p role="status">{eligible.length} reclaimable record(s){eligible.length > 64 ? "; the first 64 are reclaimed per transaction" : ""}; {kept.length} finished record(s) kept.</p>}
    {plan && <ul className="list-disc ml-5">
      {eligible.map((r) => <li key={r.deployment.deploymentId}>{r.deployment.componentName} {r.deployment.version}: {r.reason}</li>)}
      {kept.map((r) => <li key={r.deployment.deploymentId} className="text-amber-700">{r.deployment.componentName} {r.deployment.version}: kept, {r.reason}</li>)}
    </ul>}
    {eligible.length > 0 && <div className="flex gap-2">
      <button type="button" onClick={() => void reclaim.dryRun()}>Dry run</button>
      <button type="button" disabled={!reclaim.preview?.ok} onClick={() => void reclaim.sign()}>Sign and reclaim</button>
    </div>}
    {reclaim.preview && <PreviewLine preview={reclaim.preview} />}
    {reclaim.result && <p role="status">{reclaim.result}</p>}
    {reclaim.error && <p role="alert">{reclaim.error}</p>}
  </fieldset>;
}
