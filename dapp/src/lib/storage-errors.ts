/** Storage read failures must never expose query keys, object IDs or transport internals.
 * Missing optional records are successful null results, not errors to format. */
export function formatStorageQueryError(_error: unknown, subject = "Fleet storage"): string {
  return subject + " could not be loaded. Try again. If the problem continues, check the selected fleet and network.";
}
