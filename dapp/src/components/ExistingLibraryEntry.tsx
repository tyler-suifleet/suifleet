"use client";

import Link from "next/link";
import type { ComponentVersion } from "@/lib/catalog";
import { catalogLabel, existingVersions, sameCatalog } from "@/lib/component-library";

/** Shows the library entries that already use a name and version, so a blocked publication can be inspected or deployed instead. */
export function ExistingLibraryEntry({ versions, catalog, name, version }: { versions: ComponentVersion[]; catalog: string; name: string; version: string }) {
  const existing = existingVersions(versions, name, version);
  if (!existing.length) return null;
  return <section aria-label="Existing library entry" className="rounded border border-amber-300 bg-amber-50 p-3 text-sm space-y-2 break-words"><p className="font-medium">{name} {version} is already in your library</p>{existing.map(v => <div key={v.objectId} className="space-y-1"><p>{catalogLabel(v.catalogId)}{sameCatalog(v.catalogId, catalog) ? " · selected publisher catalog" : " · another of your publisher catalogs"} · {v.manifests.map(m => `${m.os}/${m.arch}`).join(", ")}</p><Link href={`/firmware?componentVersion=${v.objectId}`} className="text-blue-700 underline">Deploy the existing version</Link><details className="text-xs text-gray-500"><summary className="cursor-pointer">Technical details</summary><p className="break-all">Version object: {v.objectId}</p><p className="break-all">Catalog: {v.catalogId}</p></details></div>)}<p>Publish a new version number for changed software.</p></section>;
}
