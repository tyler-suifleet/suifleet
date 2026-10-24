// Shadow documents in encrypted fleet archives (SF-084).
//
// A schema-2 archive carries the device's shadow documents (desired, reported
// and their delta) as the device signed them, encrypted under the fleet
// owner/support Seal policy. They never enter public health. Parsing is
// strict and mirrors suifleet-edge/src/fleet_data.rs `ShadowSnapshot`
// (interfaces/compat/archive-shadow-cases.json): a malformed document, or one
// whose delta does not follow from its sections, rejects the whole archive.
// A document holding integers beyond this browser's exact range is valid for
// the device but cannot be shown faithfully here, so it is only marked
// unavailable.

export const MAX_SHADOWS = 128;
export const MAX_SHADOW_DEPTH = 8;

export type JsonObject = { [key: string]: JsonValue };
export type JsonValue = string | number | boolean | JsonValue[] | JsonObject;

export interface ShadowDocument {
  /** `""` is the classic shadow. */
  name: string;
  version: number;
  updated_ms: number;
  desired: JsonObject | null;
  reported: JsonObject | null;
  delta: JsonObject;
}
export interface ShadowSnapshot { documents: ShadowDocument[]; omitted: number }

const safe = (n: unknown): n is number => typeof n === 'number' && Number.isSafeInteger(n) && n >= 0;
const exact = (v: object, fields: string[]) => Object.keys(v).sort().join() === [...fields].sort().join();
const isObject = (v: unknown): v is JsonObject => !!v && typeof v === 'object' && !Array.isArray(v);
const own = (o: JsonObject, key: string) => Object.prototype.hasOwnProperty.call(o, key);

/** No null anywhere, finite numbers, bounded depth (the daemon's `check_document`). */
function plainValue(value: unknown, depth: number): value is JsonValue {
  if (value === null) return false;
  if (typeof value === 'number') return Number.isFinite(value);
  if (typeof value === 'string' || typeof value === 'boolean') return true;
  if (typeof value !== 'object' || depth >= MAX_SHADOW_DEPTH) return false;
  return Object.values(value as object).every((v) => plainValue(v, depth + 1));
}
const section = (v: unknown): v is JsonObject | null => v === null || (isObject(v) && plainValue(v, 0));
/** Whether every integer survived JSON parsing here exactly. */
function exactHere(value: unknown): boolean {
  if (typeof value === 'number') return !Number.isInteger(value) || Number.isSafeInteger(value);
  return !value || typeof value !== 'object' || Object.values(value).every(exactHere);
}
export const isExactHere = (d: ShadowDocument) => exactHere(d.desired) && exactHere(d.reported) && exactHere(d.delta);

/** JSON equality as the daemon judges it (exact integers, 1 equals 1.0). */
export function jsonEqual(a: JsonValue, b: JsonValue): boolean {
  if (Array.isArray(a) || Array.isArray(b)) {
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => jsonEqual(v, b[i]));
  }
  if (isObject(a) || isObject(b)) {
    if (!isObject(a) || !isObject(b)) return false;
    const keys = Object.keys(a);
    return keys.length === Object.keys(b).length && keys.every((k) => own(b, k) && jsonEqual(a[k], b[k]));
  }
  return a === b;
}

/** The desired keys that differ from reported (the daemon shadow store's `delta`). */
export function shadowDelta(desired: JsonObject | null, reported: JsonObject | null): JsonObject {
  const out: JsonObject = Object.create(null);
  for (const [key, value] of Object.entries(desired ?? {})) {
    const other = reported && own(reported, key) ? reported[key] : undefined;
    if (isObject(value) && isObject(other)) {
      const nested = shadowDelta(value, other);
      if (Object.keys(nested).length) Object.defineProperty(out, key, { value: nested, enumerable: true, writable: true });
    } else if (other === undefined || !jsonEqual(value, other)) {
      Object.defineProperty(out, key, { value, enumerable: true, writable: true });
    }
  }
  return out;
}

/** The `shadows` field of a schema-2 archive, or throws. */
export function parseShadowSnapshot(value: unknown): ShadowSnapshot {
  if (!isObject(value) || !exact(value, ['documents', 'omitted']) || !safe(value.omitted)
    || !Array.isArray(value.documents) || value.documents.length > MAX_SHADOWS) throw Error('Invalid archived shadow snapshot');
  let previous: string | undefined;
  for (const d of value.documents as unknown[]) {
    if (!isObject(d) || !exact(d, ['name', 'version', 'updated_ms', 'desired', 'reported', 'delta'])
      || typeof d.name !== 'string' || !/^[A-Za-z0-9:_-]{0,64}$/.test(d.name)
      || (previous !== undefined && !(previous < d.name)) || !safe(d.version) || !safe(d.updated_ms)
      || !section(d.desired) || !section(d.reported) || !isObject(d.delta) || !plainValue(d.delta, 0)
      || (isExactHere(d as unknown as ShadowDocument) && !jsonEqual(d.delta, shadowDelta(d.desired, d.reported)))) throw Error('Invalid archived shadow document');
    previous = d.name;
  }
  return value as unknown as ShadowSnapshot;
}

export type DocumentView = ({ state: 'verified' } & ShadowDocument) | { state: 'unavailable'; name: string; reason: string };
export type ShadowDocumentsView =
  | { state: 'verified'; documents: DocumentView[]; omitted: number; seq: number; observedMs: number; newest: boolean }
  | { state: 'unavailable'; reason: string };

/**
 * The documents a verified archive carries. Only the device's newest archive
 * describes its current documents; an older one is labeled as history.
 */
export function shadowDocumentsView(archive: { schema: number; seq: number; observed_ms: number; shadows?: ShadowSnapshot },
  index: number, anchorCount: number): ShadowDocumentsView {
  if (![2,3,4].includes(archive.schema) || !archive.shadows) {
    return { state: 'unavailable', reason: 'This archive has no shadow documents: the device had no shadow service enabled, or its daemon predates archived shadows.' };
  }
  const documents = archive.shadows.documents.map((d): DocumentView => isExactHere(d) ? { state: 'verified', ...d }
    : { state: 'unavailable', name: d.name, reason: 'It holds integers beyond the exact range of browser numbers, which cannot be shown faithfully.' });
  return { state: 'verified', documents, omitted: archive.shadows.omitted, seq: archive.seq,
    observedMs: archive.observed_ms, newest: index === anchorCount - 1 };
}
