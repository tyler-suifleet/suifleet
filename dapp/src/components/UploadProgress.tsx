import type { UploadProgress as Progress } from "@/lib/walrus";

/** Percent describes bytes sent, not Walrus storage/certification. */
export function UploadProgress({ progress, label = "Uploading to Walrus…" }: { progress: Progress | null; label?: string }) {
  const percent = progress?.phase === "uploading" && progress.total && progress.total > 0
    ? Math.min(100, Math.floor(100 * progress.loaded / progress.total)) : undefined;
  const detail = progress?.phase === "preparing" ? "Preparing upload…"
    : progress?.phase === "processing" ? "Upload sent · waiting for Walrus…"
    : progress?.phase === "complete" ? "Stored on Walrus"
    : percent === undefined ? label : `${label} ${percent}% sent`;
  return <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100}
    aria-valuenow={progress?.phase === "complete" ? 100 : percent} aria-valuetext={detail}
    className="relative overflow-hidden rounded border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-950">
    <div aria-hidden="true" className={`absolute inset-y-0 left-0 bg-blue-200 ${percent === undefined && progress?.phase !== "complete" ? "w-full animate-pulse" : ""}`}
      style={percent !== undefined || progress?.phase === "complete" ? { width: `${percent ?? 100}%` } : undefined} />
    <span className="relative z-10">{detail}</span>
  </div>;
}
