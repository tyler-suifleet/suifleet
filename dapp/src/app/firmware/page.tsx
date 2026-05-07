"use client";

import { useState, useCallback } from "react";
import { useCurrentAccount, useSignAndExecuteTransaction } from "@mysten/dapp-kit";
import { useGroups } from "@/hooks/useGroups";
import { uploadFirmware, type UploadResult } from "@/lib/walrus";
import { buildCreateDeploymentTx } from "@/lib/sui";
import { PageHeader } from "@/app/page";
import Link from "next/link";

type Step = "upload" | "deploy" | "done";

export default function FirmwarePage() {
  const account = useCurrentAccount();
  const { data: groups = [] } = useGroups();
  const { mutate: signAndExecute, isPending: txPending } = useSignAndExecuteTransaction();

  const [step, setStep] = useState<Step>("upload");
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<UploadResult | null>(null);
  const [firmwareName, setFirmwareName] = useState("");
  const [version, setVersion] = useState("");
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set());
  const [txDigest, setTxDigest] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const handleFile = useCallback(async (file: File) => {
    if (!firmwareName || !version) {
      alert("Enter component name and version before uploading.");
      return;
    }
    setUploading(true);
    try {
      const result = await uploadFirmware(file);
      setUploadResult(result);
      setStep("deploy");
    } catch (err) {
      alert(`Upload failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setUploading(false);
    }
  }, [firmwareName, version]);

  function toggleGroup(id: string) {
    setSelectedGroups((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  function handleDeploy() {
    if (!uploadResult || selectedGroups.size === 0) return;
    const tx = buildCreateDeploymentTx(
      firmwareName, version,
      uploadResult.blobId, uploadResult.sha256,
      Array.from(selectedGroups),
    );
    signAndExecute(
      { transaction: tx },
      {
        onSuccess: (res) => { setTxDigest(res.digest); setStep("done"); },
        onError: (err) => alert(`Deployment failed: ${err.message}`),
      }
    );
  }

  if (!account) return <p className="text-sm text-gray-500">Connect your wallet to deploy firmware.</p>;

  if (step === "done" && txDigest) {
    return (
      <div className="max-w-2xl space-y-4">
        <PageHeader title="Deploy firmware" />
        <div className="bg-white border border-gray-200 rounded p-8 text-center space-y-3">
          <div className="w-12 h-12 bg-green-100 rounded-full flex items-center justify-center mx-auto">
            <svg className="w-6 h-6 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
            </svg>
          </div>
          <p className="font-semibold text-gray-800">Deployment created successfully</p>
          <p className="text-xs font-mono text-gray-400">{txDigest}</p>
          <div className="flex gap-3 justify-center pt-2">
            <Link href="/deployments" className="text-sm text-[#4DA2FF] hover:underline">View deployments</Link>
            <button
              onClick={() => { setStep("upload"); setUploadResult(null); setSelectedGroups(new Set()); setTxDigest(null); }}
              className="text-sm text-gray-500 hover:text-gray-700"
            >
              Deploy another
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-2xl space-y-4">
      <PageHeader title="Deploy firmware" subtitle="Upload a firmware component to Walrus and deploy it to device groups" />

      <div className="flex items-center gap-2 text-sm">
        <StepPip n={1} active={step === "upload"} done={step !== "upload"} label="Upload" />
        <div className="flex-1 h-px bg-gray-200" />
        <StepPip n={2} active={step === "deploy"} done={step === "done"} label="Target groups" />
      </div>

      {/* Step 1 */}
      <div className={`bg-white border rounded p-6 space-y-4 ${step !== "upload" ? "opacity-50 pointer-events-none border-gray-100" : "border-gray-200"}`}>
        <h2 className="text-sm font-semibold text-gray-700">1. Component details &amp; upload</h2>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Component name" value={firmwareName} onChange={setFirmwareName} placeholder="my-firmware" disabled={step !== "upload"} />
          <Field label="Version" value={version} onChange={setVersion} placeholder="1.0.0" disabled={step !== "upload"} />
        </div>
        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => { e.preventDefault(); setDragOver(false); const f = e.dataTransfer.files[0]; if (f) handleFile(f); }}
          className={`border-2 border-dashed rounded-lg p-8 text-center transition-colors ${
            step !== "upload" ? "border-gray-100 bg-gray-50 cursor-not-allowed" :
            dragOver ? "border-[#4DA2FF] bg-blue-50" :
            "border-gray-300 hover:border-[#4DA2FF] cursor-pointer"
          }`}
        >
          {uploading ? (
            <p className="text-sm text-gray-500">Uploading to Walrus…</p>
          ) : (
            <>
              <p className="text-sm text-gray-500">Drop .swu file here</p>
              <label className="mt-2 inline-block text-sm text-[#4DA2FF] cursor-pointer hover:underline">
                or browse
                <input type="file" className="hidden" disabled={step !== "upload"} onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFile(f); }} />
              </label>
            </>
          )}
        </div>
        {uploadResult && (
          <div className="bg-gray-50 border border-gray-200 rounded p-3 text-xs font-mono space-y-1">
            <p><span className="text-gray-400">Blob ID:</span> <span className="text-gray-700">{uploadResult.blobId}</span></p>
            <p><span className="text-gray-400">SHA256: </span> <span className="text-gray-700">{uploadResult.sha256}</span></p>
          </div>
        )}
      </div>

      {/* Step 2 */}
      <div className={`bg-white border rounded p-6 space-y-4 ${step !== "deploy" ? "opacity-50 pointer-events-none border-gray-100" : "border-gray-200"}`}>
        <h2 className="text-sm font-semibold text-gray-700">2. Select target device groups</h2>
        {groups.length === 0 ? (
          <p className="text-sm text-gray-400">
            No device groups yet.{" "}
            <Link href="/devices" className="text-[#4DA2FF] hover:underline">Create a group first.</Link>
          </p>
        ) : (
          <div className="border border-gray-200 rounded divide-y divide-gray-100">
            {groups.map((g) => (
              <label key={g.objectId} className="flex items-center gap-3 px-4 py-3 cursor-pointer hover:bg-gray-50">
                <input type="checkbox" checked={selectedGroups.has(g.objectId)} onChange={() => toggleGroup(g.objectId)} className="rounded" />
                <div className="flex-1">
                  <p className="text-sm font-medium">{g.name}</p>
                  <p className="text-xs text-gray-400">{g.deviceIds.length} device{g.deviceIds.length !== 1 ? "s" : ""}</p>
                </div>
              </label>
            ))}
          </div>
        )}
        <button
          onClick={handleDeploy}
          disabled={txPending || selectedGroups.size === 0 || !uploadResult}
          className="bg-[#4DA2FF] hover:bg-[#2e8ed4] text-white px-5 py-2 rounded text-sm disabled:opacity-40"
        >
          {txPending ? "Submitting…" : `Deploy to ${selectedGroups.size} group${selectedGroups.size !== 1 ? "s" : ""}`}
        </button>
      </div>
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
