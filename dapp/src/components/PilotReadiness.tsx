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
  return <section aria-label="Pilot readiness" className="space-y-3">
    <p className="text-slate-600">Check capacity before adding funds. Device capacity, account fees, device reporting funds and storage funds are separate.</p>
    <div className="grid gap-3 md:grid-cols-3">
      <article className="rounded-lg border border-slate-200 bg-white p-4"><h3 className="font-semibold">1. Device capacity</h3>
        <p>{!connected ? "Connect and choose a fleet to check your organization’s device limit." : !capacity ? "Check the device limit enabled for this workspace." : `${capacity.used} of ${capacity.limit} device places used.`}</p>
        {capacity && <p className="mt-1 text-slate-600">{!capacity.administrator ? "An organization administrator must register devices." : capacity.used >= capacity.limit ? "No places available for another device. Increase capacity, or use a device already registered." : "Capacity is available for another device."}</p>}
        <Link className={link} href="/entitlements">Check device capacity →</Link>
        <p className="mt-1 text-xs text-slate-600">A subscription can provide capacity where checkout is enabled. Adding to the device operating budget cannot raise this limit.</p>
      </article>
      <article className="rounded-lg border border-slate-200 bg-white p-4"><h3 className="font-semibold">2. Account approval fees</h3>
        <p>{!connected ? "Connect to check the account used to approve changes." : balanceFailed ? "Account balance could not be verified. Refresh before sending funds." : balance === undefined ? "Checking account balance…" : BigInt(balance) === BigInt(0) ? "This account needs SUI for the network fee when you register or deploy." : `${displaySui(balance)} available for actions you approve.`}</p>
        <a className={link} href="#step-funding">Check account approval fees →</a>
        <p className="mt-1 text-xs text-slate-600">The wallet shows the actual fee before approval. Testnet uses free test tokens.</p>
      </article>
      <article className="rounded-lg border border-slate-200 bg-white p-4"><h3 className="font-semibold">3. Device operating budget</h3>
        <p>{!connected ? "Devices use a separate device operating budget to send reports." : budgetFailed ? "Device operating budget could not be verified. Check its page before depositing again." : !budgetChecked ? "Checking the device operating budget…" : deviceBudget === undefined ? "No device operating budget is available here yet. Create one on the Device and storage budgets page." : deviceBudget === BigInt(0) ? "The device operating budget is empty. Fund it before expecting device reports." : `${displaySui(deviceBudget)} in the device operating budget. Check spending limits and device enrollment before expecting reports.`}</p>
        <Link className={link} href="/budget">Check device operating budget →</Link>
        <p className="mt-1 text-xs text-slate-600">The storage budget funds files and logs separately. Logs can stay disabled for your first update.</p>
      </article>
    </div>
    <p className="text-slate-600">Then install the device client, register its public address and send a small test update. A registered device is not online until it reports; a created deployment is not installed until the device confirms its result.</p>
  </section>;
}
