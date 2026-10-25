import type { PriceTable } from './fleet-pricing';
const MONTH_MS = 365.25 / 12 * 86400000;

/** Proposed monthly price, decimal GB. The included cohort gets its first
 * retention-days of storage free too. Excess retention remains chargeable.
 * This is a steady-state plan estimate, never an invoice for lifetime counters.
 */
export function dataPrice(table: PriceTable, devices: number, bytesPerDeviceMonth: number, retentionDays: number) {
  if (![devices, bytesPerDeviceMonth].every(n => Number.isSafeInteger(n) && n >= 0) || !Number.isFinite(retentionDays) || retentionDays < 0) throw Error('Invalid data workload');
  const written = devices * bytesPerDeviceMonth, allowance = devices * table.data.includedBytesPerDeviceMonth;
  if (!Number.isSafeInteger(written) || !Number.isSafeInteger(allowance)) throw Error('Data workload exceeds safe range');
  const included = Math.min(written, allowance), monthDays = MONTH_MS / 86400000;
  const storedGBMonth = written / 1e9 * retentionDays / monthDays;
  const billableGBMonth = Math.max(0, storedGBMonth - included / 1e9 * Math.min(retentionDays, table.data.includedRetentionDays) / monthDays);
  const writeUsd = Math.max(0, written - allowance) / 1e9 * table.data.writeUsdPerGB;
  const storageUsd = billableGBMonth * table.data.storageUsdPerGBMonth;
  return { written, allowance, storedGBMonth, billableGBMonth, writeUsd, storageUsd, monthlyUsd: writeUsd + storageUsd };
}
