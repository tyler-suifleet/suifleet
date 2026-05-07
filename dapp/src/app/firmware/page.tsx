"use client";

import { useState, useCallback } from "react";
import { useCurrentAccount, useSignAndExecuteTransaction } from "@mysten/dapp-kit";
import { useGroups } from "@/hooks/useGroups";
import { useDevices } from "@/hooks/useDevices";
import { uploadArtifact, uploadRecipe, type ComponentRecipe } from "@/lib/walrus";
import { buildCreateDeploymentTx } from "@/lib/sui";
import { PageHeader } from "@/app/page";
import Link from "next/link";

type Step = "artifact" | "recipe" | "deploy" | "done";

interface ArtifactResult {
  blobId: string;
  sha256: string;
  fileName: string;
}

export default function FirmwarePage() {
  const account = useCurrentAccount();
  const { data: groups = [] } = useGroups();
  const { data: devices = [] } = useDevices();
  const { mutate: signAndExecute, isPending: txPending } = useSignAndExecuteTransaction();

  const [step, setStep] = useState<Step>("artifact");
  const [uploading, setUploading] = useState(false);
  const [artifact, setArtifact] = useState<ArtifactResult | null>(null);
  const [componentName, setComponentName] = useState("");
  const [version, setVersion] = useState("");
  const [description, setDescription] = useState("");
  const [installCmd, setInstallCmd] = useState("");
  const [runCmd, setRunCmd] = useState("");
  const [shutdownCmd, setShutdownCmd] = useState("");
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set());
  const [selectedDevices, setSelectedDevices] = useState<Set<string>>(new Set());
  const [txDigest, setTxDigest] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const handleFile = useCallback(async (file: File) => {
    if (!componentName || !version) {
      alert("Enter component name and version before uploading.");
      return;
    }
    setUploading(true);
    try {
      const result = await uploadArtifact(file);
      setArtifact({ ...result, fileName: file.name });
      setStep("recipe");
    } catch (err) {
      alert(`Upload failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setUploading(false);
    }
  }, [componentName, version]);

  async function handleDeploy() {
    if (!artifact || selectedGroups.size === 0) return;

    const recipe: ComponentRecipe = {
      schemaVersion: "1.0",
      componentName,
      componentVersion: version,
      ...(description ? { description } : {}),
      artifactBlobId: artifact.blobId,
      artifactSha256: artifact.sha256,
      lifecycle: {
        ...(installCmd ? { install: installCmd } : {}),
        ...(runCmd ? { run: runCmd } : {}),
        ...(shutdownCmd ? { shutdown: shutdownCmd } : {}),
      },
      configuration: {},
    };

    let recipeBlobId: string;
    try {
      recipeBlobId = await uploadRecipe(recipe);
    } catch (err) {
      alert(`Recipe upload failed: ${err instanceof Error ? err.message : String(err)}`);
      return;
    }

    const tx = buildCreateDeploymentTx(
      componentName, version,
      recipeBlobId, artifact.sha256,
      Array.from(selectedGroups),
      Array.from(selectedDevices),
    );
    signAndExecute(
      { transaction: tx },
      {
        onSuccess: (res) => { setTxDigest(res.digest); setStep("done"); },
        onError: (err) => alert(`Deployment failed: ${err.message}`),
      }
    );
  }

  function toggle(set: Set<string>, setter: (s: Set<string>) => void, id: string) {
    const next = new Set(set);
    next.has(id) ? next.delete(id) : next.add(id);
    setter(next);
  }

  if (!account) return <p className="text-sm text-gray-500">Connect your wallet to deploy a component.</p>;

  if (step === "done" && txDigest) {
    return (
      <div className="max-w-2xl space-y-4">
        <PageHeader title="Deploy component" />
        <div className="bg-white border border-gray-200 rounded p-8 text-center space-y-3">
          <div className="w-12 h-12 bg-green-100 rounded-full flex items-center justify-center mx-auto">
            <svg className="w-6 h-6 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
            </svg>
          </div>
          <p className="font-semibold text-gray-800">Deployment created</p>
          <p className="text-xs font-mono text-gray-400">{txDigest}</p>
          <div className="flex gap-3 justify-center pt-2">
            <Link href="/deployments" className="text-sm text-[#4DA2FF] hover:underline">View deployments</Link>
            <button
              onClick={() => {
                setStep("artifact"); setArtifact(null);
                setComponentName(""); setVersion(""); setDescription("");
                setInstallCmd(""); setRunCmd(""); setShutdownCmd("");
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

  const steps: { key: Step; label: string }[] = [
    { key: "artifact", label: "Artifact" },
    { key: "recipe",   label: "Recipe" },
    { key: "deploy",   label: "Target groups" },
  ];

  return (
    <div className="max-w-2xl space-y-4">
      <PageHeader title="Deploy component" subtitle="Upload an artifact, define its lifecycle recipe, and deploy to device groups" />

      {/* Step indicators */}
      <div className="flex items-center gap-2 text-sm">
        {steps.map((s, i) => (
          <>
            <StepPip
              key={s.key}
              n={i + 1}
              active={step === s.key}
              done={steps.findIndex((x) => x.key === step) > i}
              label={s.label}
            />
            {i < steps.length - 1 && <div key={`div-${i}`} className="flex-1 h-px bg-gray-200" />}
          </>
        ))}
      </div>

      {/* Step 1 — Artifact */}
      <Card disabled={step !== "artifact"} title="1. Component details & artifact">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Component name" value={componentName} onChange={setComponentName} placeholder="my-app" disabled={step !== "artifact"} />
          <Field label="Version" value={version} onChange={setVersion} placeholder="1.0.0" disabled={step !== "artifact"} />
        </div>
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
            <p className="text-sm text-gray-500">Uploading to Walrus…</p>
          ) : artifact ? (
            <p className="text-sm text-gray-600 font-medium">{artifact.fileName} <span className="text-gray-400 font-normal">— uploaded</span></p>
          ) : (
            <>
              <p className="text-sm text-gray-500">Drop any file here</p>
              <p className="text-xs text-gray-400 mt-1">Binaries, scripts, archives, .swu — anything</p>
              <label className="mt-2 inline-block text-sm text-[#4DA2FF] cursor-pointer hover:underline">
                or browse
                <input type="file" className="hidden" disabled={step !== "artifact"} onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFile(f); }} />
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
      </Card>

      {/* Step 2 — Recipe */}
      <Card disabled={step !== "recipe"} title="2. Lifecycle recipe">
        <Field label="Description" value={description} onChange={setDescription} placeholder="Optional" disabled={step !== "recipe"} />
        <div className="space-y-3 mt-1">
          <CmdField
            label="Install"
            hint="Runs once after artifact is downloaded. Use {artifacts} for the artifact path."
            value={installCmd}
            onChange={setInstallCmd}
            placeholder="chmod +x {artifacts}/my-app"
            disabled={step !== "recipe"}
          />
          <CmdField
            label="Run"
            hint="Starts the component process. Leave blank for one-shot artifacts."
            value={runCmd}
            onChange={setRunCmd}
            placeholder="{artifacts}/my-app --config /etc/my-app/config.json"
            disabled={step !== "recipe"}
          />
          <CmdField
            label="Shutdown"
            hint="Gracefully stops the component. Leave blank if not applicable."
            value={shutdownCmd}
            onChange={setShutdownCmd}
            placeholder="pkill my-app"
            disabled={step !== "recipe"}
          />
        </div>
        {step === "recipe" && (
          <button
            onClick={() => setStep("deploy")}
            className="mt-2 bg-[#4DA2FF] hover:bg-[#2e8ed4] text-white px-5 py-2 rounded text-sm"
          >
            Continue to target groups
          </button>
        )}
      </Card>

      {/* Step 3 — Targets */}
      <Card disabled={step !== "deploy"} title="3. Select targets">
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
          disabled={txPending || (selectedGroups.size === 0 && selectedDevices.size === 0) || !artifact || step !== "deploy"}
          className="mt-2 bg-[#4DA2FF] hover:bg-[#2e8ed4] text-white px-5 py-2 rounded text-sm disabled:opacity-40"
        >
          {txPending ? "Submitting…" : `Deploy to ${selectedGroups.size + selectedDevices.size} target${selectedGroups.size + selectedDevices.size !== 1 ? "s" : ""}`}
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
