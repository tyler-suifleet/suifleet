"use client";

import { Fragment, useState, useCallback, useEffect, useRef } from "react";
import { useCurrentAccount } from "@mysten/dapp-kit-react";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { useGroups } from "@/hooks/useGroups";
import { useDevices } from "@/hooks/useDevices";
import { uploadArtifact, uploadRecipe, renewalCommand, storageWarnings, type ComponentRecipe, type Protection, type StorageInfo, type UploadProgress as Progress } from "@/lib/walrus";
import { DEFAULT_DELIVERY, releaseProtection } from "@/lib/delivery";
import { useAccessPolicies } from "@/hooks/useAccessPolicies";
import { useFleetBlobStore } from "@/hooks/useFleetStorage";
import { FleetStorageChoice } from "@/components/FleetStorageChoice";
import type { BlobStore } from "@/lib/walrus";
import { UploadProgress } from "@/components/UploadProgress";
import { DeploymentProgress, type DeploymentPhase } from "@/components/DeploymentProgress";
import { DeliveryFields } from "@/components/DeliveryFields";
import { buildCreateDeploymentTx } from "@/lib/sui";
import { buildDesiredRevisionTx } from "@/lib/desired-state";
import { fetchVersion, buildCatalogDeploymentTx, type ComponentVersion } from "@/lib/catalog";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { assertComponentRecipe } from "@/lib/recipe-schema";
import { EMPTY_CONFIGURATION, recipeConfiguration } from "@/lib/recipe-configuration";
import { PageHeader } from "@/components/ui";
import { ConfigurationFields } from "@/components/ConfigurationFields";
import Link from "next/link";

type Step = "artifact" | "recipe" | "deploy" | "done";

interface ArtifactResult {
  blobId: string;
  sha256: string;
  fileName: string;
  storage: StorageInfo;
  confidential: boolean;
}

export default function FirmwarePage() {
  const account = useCurrentAccount();
  const client = useCurrentClient();
  const [catalogVersion, setCatalogVersion] = useState<ComponentVersion | null>(null);
  const [catalogError, setCatalogError] = useState("");
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("componentVersion");
    if (id) fetchVersion(client, id).then(v => { setCatalogVersion(v); setComponentName(v.name); setVersion(v.version); setStep("deploy"); }).catch(e => setCatalogError(String(e)));
  }, [client]);
  const { data: groups = [] } = useGroups();
  const { data: devices = [] } = useDevices();
  const { data: policies = [] } = useAccessPolicies();
  const [delivery, setDelivery] = useState(DEFAULT_DELIVERY);
  // One release identity: the recipe reuses the artifact's protection.
  const [protection, setProtection] = useState<Protection | null>(null);
  // Fleet-paid storage (SF-083), chosen once for the artifact and its recipe.
  const fleet = useFleetBlobStore();
  const [fleetPaid, setFleetPaid] = useState(false);
  const [store, setStore] = useState<BlobStore | undefined>(undefined);
  const { mutateAsync: signAndExecute, isPending: txPending } = useFleetTransaction();

  const [step, setStep] = useState<Step>("artifact");
  const [uploading, setUploading] = useState(false);
  const uploadInFlight = useRef(false);
  const deploymentInFlight = useRef(false);
  const [uploadProgress, setUploadProgress] = useState<Progress | null>(null);
  const [deployPhase, setDeployPhase] = useState<DeploymentPhase | null>(null);
  const [deploymentStartedAt, setDeploymentStartedAt] = useState(0);
  const [recipeProgress, setRecipeProgress] = useState<Progress | null>(null);
  const [artifact, setArtifact] = useState<ArtifactResult | null>(null);
  const [componentName, setComponentName] = useState("");
  const [version, setVersion] = useState("");
  const [description, setDescription] = useState("");
  const [installCmd, setInstallCmd] = useState("");
  const [runCmd, setRunCmd] = useState("");
  const [shutdownCmd, setShutdownCmd] = useState("");
  const [readinessCmd, setReadinessCmd] = useState("");
  const [restartPolicy, setRestartPolicy] = useState<"on-failure" | "never" | "always">("on-failure");
  const [configuration, setConfiguration] = useState(EMPTY_CONFIGURATION);
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set());
  const [selectedDevices, setSelectedDevices] = useState<Set<string>>(new Set());
  // Desired state: the groups' current version of this component, superseding
  // earlier ones and reaching devices that join later. Groups only.
  const [desired, setDesired] = useState(true);
  const [txDigest, setTxDigest] = useState<string | null>(null);
  const [recipeStorage, setRecipeStorage] = useState<StorageInfo | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const handleFile = useCallback(async (file: File) => {
    if (uploadInFlight.current || step !== "artifact") return;
    if (!componentName || !version) {
      alert("Enter component name and version before uploading.");
      return;
    }
    uploadInFlight.current = true;
    setUploading(true);
    setUploadProgress(null);
    try {
      const release = releaseProtection(client, delivery, policies);
      // Fleet-paid storage is never silently replaced by the publisher.
      if (fleetPaid && !fleet.store) throw new Error(`Fleet storage is unavailable: ${fleet.reason ?? "unknown reason"}`);
      const chosen = fleetPaid ? fleet.store : undefined;
      const result = await uploadArtifact(file, account?.address ?? null, release, setUploadProgress, chosen);
      setProtection(release);
      setStore(() => chosen);
      setArtifact({ ...result, fileName: file.name });
      setStep("recipe");
    } catch (err) {
      alert(`Upload failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      uploadInFlight.current = false;
      setUploading(false);
    }
  }, [componentName, version, account?.address, client, delivery, policies, step, fleetPaid, fleet.store, fleet.reason]);

  const desiredTargets = desired && selectedDevices.size === 0 && selectedGroups.size > 0;

  const [initiallyPaused, setInitiallyPaused] = useState(false);

  async function handleDeploy() {
    // A ref closes the gap before React renders busy state, including the
    // entire recipe upload; mutation.isPending starts too late for that.
    if (deploymentInFlight.current || step !== "deploy" || (selectedGroups.size === 0 && selectedDevices.size === 0)) return;
    if (!catalogVersion && (!artifact || !protection)) return;
    if (!catalogVersion && desired && selectedDevices.size > 0) {
      alert("Desired state targets groups only. Clear the individual devices, or untick desired state for a one-shot deployment.");
      return;
    }
    deploymentInFlight.current = true;
    setDeploymentStartedAt(Date.now());
    setDeployPhase("preparing");
    setRecipeProgress(null);
    let succeeded = false;
    try {
      let transaction;
      if (catalogVersion) {
        transaction = buildCatalogDeploymentTx(catalogVersion.objectId, [...selectedGroups], [...selectedDevices], initiallyPaused);
      } else {
        const recipe = buildRecipe(artifact!);
        setDeployPhase("uploading");
        const recipeUpload = await uploadRecipe(recipe, account?.address ?? null, protection!, setRecipeProgress, store);
        setRecipeStorage(recipeUpload.storage);
        transaction = desiredTargets
          ? buildDesiredRevisionTx(Array.from(selectedGroups), {
              componentName, version, initiallyPaused, recipeBlobId: recipeUpload.blobId,
              artifactSha256: artifact!.sha256, recipeSha256: recipeUpload.sha256,
            })
          : buildCreateDeploymentTx(componentName, version, recipeUpload.blobId,
              artifact!.sha256, recipeUpload.sha256, [...selectedGroups], [...selectedDevices], initiallyPaused);
      }
      const result = await signAndExecute({ transaction, onProgress: setDeployPhase });
      succeeded = true;
      setTxDigest(result.digest);
      setStep("done");
    } catch (error) {
      alert(`Deployment failed: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      // A successful operation stays guarded until explicit "Deploy another".
      if (!succeeded) deploymentInFlight.current = false;
      setDeployPhase(null);
    }
  }

  /** The recipe as entered; throws with the field at fault. */
  function buildRecipe(uploaded: ArtifactResult): ComponentRecipe {
    // Configuration (SF-015) makes it a 1.2 recipe; without it any daemon accepts it.
    const configured = recipeConfiguration(configuration);
    const recipe: ComponentRecipe = {
      schemaVersion: configured.schemaVersion,
      componentName,
      componentVersion: version,
      ...(description ? { description } : {}),
      artifactBlobId: uploaded.blobId,
      artifactSha256: uploaded.sha256,
      lifecycle: {
        ...(installCmd ? { install: installCmd } : {}),
        // Supervised component (SF-014): shutdown, readiness and restart need a run command.
        ...(runCmd ? {
          run: runCmd,
          ...(shutdownCmd ? { shutdown: shutdownCmd } : {}),
          ...(readinessCmd ? { readiness: readinessCmd } : {}),
          restart: restartPolicy,
        } : {}),
        ...(configured.validate ? { validate: configured.validate } : {}),
      },
      configuration: configured.configuration,
    };
    assertComponentRecipe(recipe);
    return recipe;
  }

  function toggle(set: Set<string>, setter: (s: Set<string>) => void, id: string) {
    const next = new Set(set);
    next.has(id) ? next.delete(id) : next.add(id);
    setter(next);
  }

  if (!account) return <p className="text-sm text-gray-500">Connect your wallet to deploy a component.</p>;

  if (step === "done" && txDigest) {
    return (
      <div className="max-w-2xl space-y-4 [overflow-wrap:anywhere]">
        <PageHeader title="Deploy component" />
        <div className="bg-white border border-gray-200 rounded p-8 text-center space-y-3">
          <div className="w-12 h-12 bg-green-100 rounded-full flex items-center justify-center mx-auto">
            <svg className="w-6 h-6 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
            </svg>
          </div>
          <p className="font-semibold text-gray-800">Deployment created</p>
          <p className="text-xs font-mono text-gray-400">{txDigest}</p>
          <div className="text-left space-y-2">
            {artifact && <p className="text-xs text-gray-600">{artifact.confidential ? "Confidential: artifact and recipe were Seal-encrypted before upload." : "Public: artifact and recipe are readable by anyone."}</p>}
            {artifact && <StorageSummary label="Artifact storage" storage={artifact.storage} />}
            {recipeStorage && <StorageSummary label="Recipe storage" storage={recipeStorage} />}
          </div>
          <div className="flex gap-3 justify-center pt-2">
            <Link href="/deployments" className="text-sm text-[#4DA2FF] hover:underline">View deployments</Link>
            <button
              onClick={() => {
                deploymentInFlight.current = false; setRecipeProgress(null); setUploadProgress(null);
                setStep("artifact"); setArtifact(null); setRecipeStorage(null); setProtection(null); setDelivery(DEFAULT_DELIVERY);
                setComponentName(""); setVersion(""); setDescription("");
                setInstallCmd(""); setRunCmd(""); setShutdownCmd(""); setReadinessCmd(""); setRestartPolicy("on-failure");
                setConfiguration(EMPTY_CONFIGURATION);
                setSelectedGroups(new Set()); setSelectedDevices(new Set()); setTxDigest(null);
              }}
              className="text-sm text-gray-500 hover:text-gray-700"
            >
              Deploy another
            </button>
          </div>
        </div>
      </div>
    );
  }

  const steps: { key: Step; label: string }[] = catalogVersion ? [{ key: "deploy", label: "Targets & review" }] : [
    { key: "artifact", label: "Artifact" },
    { key: "recipe",   label: "Recipe" },
    { key: "deploy",   label: "Target groups" },
  ];

  return (
    <div className="max-w-2xl space-y-4 [overflow-wrap:anywhere]">
      <PageHeader title="Deploy component" subtitle={catalogVersion ? "Review this published version and select deployment targets" : "Upload an artifact, define its lifecycle recipe, and deploy to device groups"} />

      {catalogError && <p role="alert">{catalogError}</p>}
      {catalogVersion && <section aria-label="Published version review" className="rounded border bg-white p-4 space-y-2"><p>{catalogVersion.name} {catalogVersion.version} · immutable catalog version. Each device selects its own platform.</p><ol className="text-sm">{catalogVersion.manifests.map((m, i) => <li key={i}>{i + 1}. {m.os}/{m.arch}</li>)}</ol><p className="text-sm text-gray-500">Review targets below before submitting. Deployment requests delivery; installed and running state require device reports.</p><Link href="/components" className="text-sm text-blue-600">Back to software library</Link></section>}
      {/* Step indicators */}
      <div className="flex items-center gap-2 text-sm">
        {steps.map((s, i) => (
          <Fragment key={s.key}>
            <StepPip
              key={s.key}
              n={i + 1}
              active={step === s.key}
              done={steps.findIndex((x) => x.key === step) > i}
              label={s.label}
            />
            {i < steps.length - 1 && <div key={`div-${i}`} className="flex-1 h-px bg-gray-200" />}
          </Fragment>
        ))}
      </div>

      {!catalogVersion && <>
      {/* Step 1 — Artifact */}
      <Card disabled={step !== "artifact" || uploading} title="1. Component details & artifact">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Component name" value={componentName} onChange={setComponentName} placeholder="my-app" disabled={step !== "artifact" || uploading} />
          <Field label="Version" value={version} onChange={setVersion} placeholder="1.0.0" disabled={step !== "artifact" || uploading} />
        </div>
        <DeliveryFields value={delivery} onChange={setDelivery} policies={policies} disabled={step !== "artifact" || uploading} />
        <FleetStorageChoice checked={fleetPaid} onChange={setFleetPaid} available={!!fleet.store} storage={fleet.storage} reason={fleet.reason} disabled={step !== "artifact" || uploading} />
        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => { e.preventDefault(); setDragOver(false); const f = e.dataTransfer.files[0]; if (f) handleFile(f); }}
          className={`border-2 border-dashed rounded-lg p-8 text-center transition-colors ${
            step !== "artifact" ? "border-gray-100 bg-gray-50 cursor-not-allowed" :
            dragOver ? "border-[#4DA2FF] bg-blue-50" :
            "border-gray-300 hover:border-[#4DA2FF] cursor-pointer"
          }`}
        >
          {uploading ? (
            <UploadProgress progress={uploadProgress} />
          ) : artifact ? (
            <p className="text-sm text-gray-600 font-medium">{artifact.fileName} <span className="text-gray-400 font-normal">— uploaded</span></p>
          ) : (
            <>
              <p className="text-sm text-gray-500">Drop any file here</p>
              <p className="text-xs text-gray-400 mt-1">Binaries, scripts, archives, .swu — anything</p>
              <label className="mt-2 inline-block text-sm text-[#4DA2FF] cursor-pointer hover:underline">
                or browse
                <input type="file" className="hidden" disabled={step !== "artifact" || uploading} onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFile(f); }} />
              </label>
            </>
          )}
        </div>
        {artifact && (
          <div className="bg-gray-50 border border-gray-200 rounded p-3 text-xs font-mono space-y-1">
            <p><span className="text-gray-400">Artifact blob:</span> <span className="text-gray-700">{artifact.blobId}</span></p>
            <p><span className="text-gray-400">SHA-256:     </span> <span className="text-gray-700">{artifact.sha256}</span></p>
          </div>
        )}
        {artifact && <StorageSummary label="Artifact storage" storage={artifact.storage} />}
      </Card>

      {/* Step 2 — Recipe */}
      <Card disabled={step !== "recipe"} title="2. Lifecycle recipe">
        <Field label="Description" value={description} onChange={setDescription} placeholder="Optional" disabled={step !== "recipe"} />
        <div className="space-y-3 mt-1">
          <CmdField
            label="Install"
            hint="Optional one-shot install; requires recipe commands enabled on the device. Use {artifact} for the file and {artifacts} for its directory."
            value={installCmd}
            onChange={setInstallCmd}
            placeholder="/usr/local/bin/install-update {artifact}"
            disabled={step !== "recipe"}
          />
          <CmdField
            label="Run"
            hint="Optional supervised process; must stay in the foreground. Reported Applied only once ready; a failed replacement rolls back to the previous version. Use {work} for its persistent directory."
            value={runCmd}
            onChange={setRunCmd}
            placeholder="exec {artifact}"
            disabled={step !== "recipe"}
          />
          {runCmd && (
            <>
              <CmdField
                label="Readiness"
                hint="Optional check repeated until it succeeds; without one, a run that survives a few seconds is ready. {pid} is the run's process."
                value={readinessCmd}
                onChange={setReadinessCmd}
                placeholder="test -e {work}/ready"
                disabled={step !== "recipe"}
              />
              <CmdField
                label="Shutdown"
                hint="Optional graceful stop before SIGTERM/SIGKILL of the whole process group. {pid} is the run's process."
                value={shutdownCmd}
                onChange={setShutdownCmd}
                placeholder="kill -TERM {pid}"
                disabled={step !== "recipe"}
              />
              <label className="block text-sm">
                <span className="text-gray-600">Restart</span>
                <select
                  value={restartPolicy}
                  onChange={(e) => setRestartPolicy(e.target.value as typeof restartPolicy)}
                  disabled={step !== "recipe"}
                  className="ml-3 border border-gray-200 rounded px-2 py-1 text-sm"
                >
                  <option value="on-failure">on failure</option>
                  <option value="always">always</option>
                  <option value="never">never</option>
                </select>
              </label>
            </>
          )}
        </div>
        <ConfigurationFields value={configuration} onChange={setConfiguration} disabled={step !== "recipe"} />
        {step === "recipe" && (
          <button
            onClick={() => {
              try {
                if (artifact) buildRecipe(artifact);
                setStep("deploy");
              } catch (err) {
                alert(err instanceof Error ? err.message : String(err));
              }
            }}
            className="mt-2 bg-[#4DA2FF] hover:bg-[#2e8ed4] text-white px-5 py-2 rounded text-sm"
          >
            Continue to target groups
          </button>
        )}
      </Card>

      </>}

      {deployPhase && <DeploymentProgress phase={deployPhase} progress={recipeProgress} startedAt={deploymentStartedAt} catalog={!!catalogVersion} />}
      {/* Step 3 — Targets */}
      <Card disabled={step !== "deploy"} title={catalogVersion ? "Select targets & review" : "3. Select targets"}>
        <label className="flex gap-2 text-sm mb-3"><input type="checkbox" checked={initiallyPaused} onChange={e => setInitiallyPaused(e.target.checked)} />
          Create paused. Configure UTC schedule, expiry, maintenance window and advance notice on Deployments, then resume.
        </label>
        <div className="space-y-4">
          {/* Groups */}
          <div>
            <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">Device groups</p>
            {groups.length === 0 ? (
              <p className="text-sm text-gray-400">No groups yet. <Link href="/devices" className="text-[#4DA2FF] hover:underline">Create one.</Link></p>
            ) : (
              <div className="border border-gray-200 rounded divide-y divide-gray-100">
                {groups.map((g) => (
                  <label key={g.objectId} className="flex items-center gap-3 px-4 py-3 cursor-pointer hover:bg-gray-50">
                    <input type="checkbox" checked={selectedGroups.has(g.objectId)} onChange={() => toggle(selectedGroups, setSelectedGroups, g.objectId)} className="rounded" disabled={step !== "deploy"} />
                    <div className="flex-1">
                      <p className="text-sm font-medium">{g.name}</p>
                      <p className="text-xs text-gray-400">{g.deviceIds.length} device{g.deviceIds.length !== 1 ? "s" : ""}</p>
                    </div>
                  </label>
                ))}
              </div>
            )}
          </div>

          {catalogVersion ? (
            <p className="text-xs text-gray-500">Catalog versions deploy one-shot; desired state does not select them yet.</p>
          ) : (
          <label className="flex items-start gap-3 text-sm">
            <input type="checkbox" checked={desired} onChange={() => setDesired(!desired)} className="rounded mt-0.5" disabled={step !== "deploy"} />
            <span>
              <span className="font-medium">Make this the groups&apos; desired version of {componentName || "this component"}</span>
              <span className="block text-xs text-gray-500">
                Devices converge on the latest desired version: offline devices skip superseded ones, devices added
                to a group later receive it, and removed devices stop following it. Untick for a one-shot deployment
                (required when targeting individual devices).
              </span>
              {desired && selectedDevices.size > 0 && (
                <span className="block text-xs text-amber-700 bg-amber-50 rounded px-2 py-1 mt-1">
                  Desired state targets groups only: clear the individual devices or untick this option.
                </span>
              )}
            </span>
          </label>
          )}

          {/* Individual devices */}
          <div>
            <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">Individual devices</p>
            {devices.length === 0 ? (
              <p className="text-sm text-gray-400">No devices registered.</p>
            ) : (
              <div className="border border-gray-200 rounded divide-y divide-gray-100">
                {devices.map((d) => (
                  <label key={d.objectId} className="flex items-center gap-3 px-4 py-3 cursor-pointer hover:bg-gray-50">
                    <input type="checkbox" checked={selectedDevices.has(d.objectId)} onChange={() => toggle(selectedDevices, setSelectedDevices, d.objectId)} className="rounded" disabled={step !== "deploy"} />
                    <div className="flex-1">
                      <p className="text-sm font-medium">{d.name}</p>
                      <p className="text-xs text-gray-400">{d.arch}</p>
                    </div>
                  </label>
                ))}
              </div>
            )}
          </div>
        </div>

        <button
          onClick={handleDeploy}
          disabled={deployPhase !== null || txPending || (selectedGroups.size === 0 && selectedDevices.size === 0) || (!artifact && !catalogVersion) || step !== "deploy"}
          className="mt-2 bg-[#4DA2FF] hover:bg-[#2e8ed4] text-white px-5 py-2 rounded text-sm disabled:opacity-40"
        >
          {deployPhase === "uploading" ? "Uploading recipe…" : deployPhase !== null || txPending ? "Deploying…" : `Deploy to ${selectedGroups.size + selectedDevices.size} target${selectedGroups.size + selectedDevices.size !== 1 ? "s" : ""}`}
        </button>
      </Card>
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

function Card({ title, disabled, children }: { title: string; disabled: boolean; children: React.ReactNode }) {
  return (
    <div className={`bg-white border rounded p-6 space-y-4 transition-opacity ${disabled ? "opacity-40 pointer-events-none border-gray-100" : "border-gray-200"}`}>
      <h2 className="text-sm font-semibold text-gray-700">{title}</h2>
      {children}
    </div>
  );
}

function StepPip({ n, active, done, label }: { n: number; active: boolean; done: boolean; label: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold shrink-0 ${done ? "bg-green-500 text-white" : active ? "bg-[#4DA2FF] text-white" : "bg-gray-200 text-gray-500"}`}>
        {done ? "✓" : n}
      </span>
      <span className={`text-xs ${active ? "text-gray-800 font-medium" : "text-gray-400"}`}>{label}</span>
    </div>
  );
}

function Field({ label, value, onChange, placeholder, disabled }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string; disabled?: boolean;
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-gray-700 mb-1">{label}</label>
      <input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} disabled={disabled}
        className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#4DA2FF] disabled:bg-gray-50" />
    </div>
  );
}

function CmdField({ label, hint, value, onChange, placeholder, disabled }: {
  label: string; hint: string; value: string; onChange: (v: string) => void; placeholder?: string; disabled?: boolean;
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-gray-700 mb-0.5">{label}</label>
      <p className="text-xs text-gray-400 mb-1">{hint}</p>
      <input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} disabled={disabled}
        className="w-full border border-gray-300 rounded px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-[#4DA2FF] disabled:bg-gray-50" />
    </div>
  );
}

/** Expiry, ownership and cost of a stored blob, with anything the operator must act on. */
function StorageSummary({ label, storage }: { label: string; storage: StorageInfo }) {
  const warnings = storageWarnings(storage);
  const renew = renewalCommand(storage);
  return (
    <div className="border border-gray-200 rounded p-3 text-xs space-y-1">
      <p className="font-medium text-gray-600">{label}</p>
      <p className="text-gray-500">
        Stored until Walrus epoch {storage.endEpoch ?? "unknown"} · renewable by{" "}
        {storage.owner === "you" ? "your wallet" : storage.owner === "fleet" ? "your fleet budget (fleet storage)" : storage.owner === "publisher" ? "the publisher only" : "another owner"}
        {storage.costFrost !== undefined && <> · cost {storage.costFrost.toString()} FROST</>}
      </p>
      {renew && storage.owner === "you" && (
        <p className="text-gray-500">
          Renew before it expires: <code className="font-mono text-gray-700">{renew}</code>
        </p>
      )}
      {warnings.map((warning) => (
        <p key={warning} className="text-amber-700 bg-amber-50 rounded px-2 py-1">{warning}</p>
      ))}
    </div>
  );
}
