"use client";

import { useEffect, useRef, useState } from "react";
import type { TransactionPhase } from "@/hooks/useFleetTransaction";
import type { UploadProgress as Progress } from "@/lib/walrus";
import { NETWORK } from "@/lib/constants";
import { UploadProgress } from "./UploadProgress";

export type DeploymentPhase = "preparing" | "uploading" | TransactionPhase;
const stages: { phase: DeploymentPhase; label: string }[] = [
  { phase: "preparing", label: "Preparing deployment" },
  { phase: "uploading", label: "Uploading recipe to Walrus" },
  { phase: "validating", label: `Checking ${NETWORK} and wallet account` },
  { phase: "wallet", label: "Wallet approval and submission" },
  { phase: "confirming", label: `Confirming transaction on ${NETWORK}` },
];

export function DeploymentProgress({ phase, progress, startedAt, catalog = false }: {
  phase: DeploymentPhase; progress: Progress | null; startedAt: number; catalog?: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    return () => element.close();
  }, []);
  useEffect(() => {
    const update = () => setElapsed(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [startedAt]);
  const visible = catalog ? stages.filter(stage => stage.phase !== "uploading") : stages;
  const current = visible.findIndex(stage => stage.phase === phase);
  return <dialog ref={dialog} aria-labelledby="deployment-progress-title" aria-describedby="deployment-progress-status"
    onCancel={event => event.preventDefault()}
    className="fixed m-auto w-[min(92vw,30rem)] rounded-xl border border-blue-200 bg-white p-6 text-gray-900 shadow-xl backdrop:bg-black/40">
    <h2 id="deployment-progress-title" className="text-lg font-semibold">Deploying component</h2>
    <p id="deployment-progress-status" role="status" className="mt-2 text-sm text-blue-800">
      {visible[current]?.label}… <span aria-live="off" className="tabular-nums">{elapsed}s elapsed</span>
    </p>
    <ol className="my-4 space-y-2 text-sm">
      {visible.map((stage, index) => <li key={stage.phase} aria-current={index === current ? "step" : undefined}
        className={index === current ? "font-semibold text-blue-800" : "text-gray-500"}>
        <span aria-hidden="true">{index < current ? "✓" : index === current ? "●" : "○"} </span>{stage.label}
      </li>)}
    </ol>
    {phase === "uploading" && <UploadProgress progress={progress} label="Uploading recipe to Walrus…" />}
    {phase === "wallet" && <p className="text-sm">Your wallet is preparing or submitting this transaction. Approve or reject when its request appears.</p>}
    {phase === "confirming" && <p className="text-sm">The wallet returned a transaction. Checking its execution result independently on {NETWORK}.</p>}
    {phase === "uploading" && progress?.phase === "processing" && <p className="mt-2 text-sm">Your upload has been sent. Walrus is still processing storage; the wallet request comes next.</p>}
  </dialog>;
}
