import Link from "next/link";
import { displaySui } from "@/lib/onboarding";

/** Read-only prerequisites: never implies a deposit, subscription or device report. */
export function PilotReadiness({ connected, capacity, balance, balanceFailed, deviceBudget, budgetChecked, budgetFailed }: {
  connected: boolean;
  capacity?: { used: bigint; limit: bigint; administrator: boolean };
  balance?: string;
  balanceFailed: boolean;
  deviceBudget?: bigint;
  budgetChecked: boolean;
  budgetFailed: boolean;
}) {
  const link = "mt-2 inline-block font-medium text-blue-700 underline underline-offset-2";
  return <section aria-label="Pilot readiness" className="rounded-xl border border-slate-200 bg-slate-50 p-5 space-y-3">
    <div><h2 className="text-lg font-semibold">Before your first device</h2>
      <p className="text-slate-600">Check capacity before adding funds. These are three separate requirements; paying one does not complete the others.</p></div>
    <div className="grid gap-3 md:grid-cols-3">
      <article className="rounded-lg border border-slate-200 bg-white p-4"><h3 className="font-semibold">1. Device capacity</h3>
        <p>{!connected ? "Connect and choose a fleet to check your organization’s device limit." : !capacity ? "Check the device limit enabled for this workspace." : `${capacity.used} of ${capacity.limit} device places used.`}</p>
        {capacity && <p className="mt-1 text-slate-600">{!capacity.administrator ? "An organization administrator must register devices." : capacity.used >= capacity.limit ? "No places available for another device. Increase capacity, or use a device already registered." : "Capacity is available for another device."}</p>}
        <Link className={link} href="/entitlements">Check device capacity →</Link>
        <p className="mt-1 text-xs text-slate-600">A subscription can provide capacity where checkout is enabled. A network deposit cannot raise this limit.</p>
      </article>
      <article className="rounded-lg border border-slate-200 bg-white p-4"><h3 className="font-semibold">2. Account fees</h3>
        <p>{!connected ? "Connect to check the account used to approve changes." : balanceFailed ? "Account balance could not be verified. Refresh before sending funds." : balance === undefined ? "Checking account balance…" : BigInt(balance) === BigInt(0) ? "This account needs SUI for the network fee when you register or deploy." : `${displaySui(balance)} available for actions you approve.`}</p>
        <a className={link} href="#funding">Check account funds →</a>
        <p className="mt-1 text-xs text-slate-600">The wallet shows the actual fee before approval. Testnet uses free test tokens.</p>
      </article>
      <article className="rounded-lg border border-slate-200 bg-white p-4"><h3 className="font-semibold">3. Device operating funds</h3>
        <p>{!connected ? "Devices use a separate fleet budget to send reports." : budgetFailed ? "Fleet budget could not be verified. Check its page before depositing again." : !budgetChecked ? "Checking the fleet budget…" : deviceBudget === undefined ? "No device budget is available here yet. Create one on Network budget." : deviceBudget === BigInt(0) ? "The fleet budget is empty. Fund it before expecting device reports." : `${displaySui(deviceBudget)} in the device budget. Check spending limits and device enrollment before expecting reports.`}</p>
        <Link className={link} href="/budget">Check network budget →</Link>
        <p className="mt-1 text-xs text-slate-600">File and log storage has separate funding. Logs can stay disabled for your first update.</p>
      </article>
    </div>
    <p className="text-slate-600">Then install the device client, register its public address and send a small test update. A registered device is not online until it reports; a created deployment is not installed until the device confirms its result.</p>
  </section>;
}
