"use client";
import Link from "next/link";
import { useFleetReporting } from "@/hooks/useFleetDataConfig";
import { useFleetSelection } from "@/hooks/useFleetSelection";

/** Missing configuration is distinct from an unfinished or failed verification. */
export function ReportingReadiness() {
  const query = useFleetReporting(), selection = useFleetSelection();
  if (!selection.supported) return <p>Reporting settings are unavailable. Ask your administrator to check this installation.</p>;
  if (selection.loading || !selection.fleet || query.isPending) return <p role="status">Checking this fleet’s reporting settings… This can take a moment.</p>;
  if (query.isError || query.isRefetchError) return <div role="alert"><p>We could not verify reporting settings. This does not mean reporting is missing.</p><button type="button" disabled={query.isFetching} onClick={() => void query.refetch()}>Retry reporting check</button></div>;
  return <p>Reporting is not set up for this fleet. An administrator can <Link href="/settings/">set up reporting in Fleet settings</Link>.</p>;
}
