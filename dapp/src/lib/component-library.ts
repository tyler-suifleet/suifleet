import { normalizeSuiObjectId } from "@mysten/sui/utils";
import type { ComponentVersion } from "./catalog";

// The contract keys versions by `name@version` within one catalog only, so a
// second catalog of the same account would accept an identical release. The
// library spans every catalog the account publishes to; uniqueness is checked
// across that whole library before anything is uploaded or signed.

export const sameCatalog = (a: string, b: string) => normalizeSuiObjectId(a) === normalizeSuiObjectId(b);

/** Library entries, from any catalog, with exactly this name and version (the on-chain key). */
export function existingVersions(library: ComponentVersion[], name: string, version: string): ComponentVersion[] {
  return library.filter(v => v.name === name && v.version === version);
}

/** Reject a name and version already published in any catalog in the library. */
export function assertVersionAvailable(library: ComponentVersion[], catalog: string, name: string, version: string) {
  const existing = existingVersions(library, name, version);
  if (!existing.length) return;
  const here = existing.some(v => sameCatalog(v.catalogId, catalog));
  throw new Error(`${name} ${version} is already in your library${here ? "" : ", published from another of your publisher catalogs"}. Published versions cannot be overwritten or repeated; choose a new version number.`);
}

/** Short, stable label distinguishing catalogs in the library; the full ID stays in technical details. */
export function catalogLabel(catalogId: string) {
  const id = normalizeSuiObjectId(catalogId);
  return `Catalog ${id.slice(0, 6)}…${id.slice(-4)}`;
}

/** Object IDs of versions whose name and version also appear in another catalog. */
export function crossCatalogDuplicates(library: ComponentVersion[]): Set<string> {
  const catalogs = new Map<string, Set<string>>();
  for (const v of library) {
    const key = JSON.stringify([v.name, v.version]);
    catalogs.set(key, (catalogs.get(key) ?? new Set()).add(normalizeSuiObjectId(v.catalogId)));
  }
  return new Set(library.filter(v => catalogs.get(JSON.stringify([v.name, v.version]))!.size > 1).map(v => v.objectId));
}
