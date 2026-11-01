"use client";

import Link from "next/link";
import { canFundEnrollment, defaultReserve } from "@/lib/fleet-budget";
import { formatToken } from "@/lib/network-costs";

const FOCUS = "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700";
const LINK = `font-medium text-blue-700 underline-offset-2 hover:underline ${FOCUS}`;

export function BudgetEnrollment({ budget, selected, onChange }: {
  budget: import("@/lib/fleet-budget").BudgetView; selected: boolean; onChange: (value: boolean) => void;
}) {
  const available = canFundEnrollment(budget);
  return <div className="space-y-1 sm:col-span-2">
    <label className="flex items-start gap-2">
      <input type="checkbox" className={`mt-0.5 ${FOCUS}`} checked={available && selected} disabled={!available}
        aria-describedby={!available ? "enrollment-funding-help" : undefined} onChange={(e) => onChange(e.target.checked)} />
      <span>Enroll in my fleet budget: send the device a {formatToken(budget.defaultLimits.floatCap)} SUI starting balance and give its own account a {formatToken(defaultReserve(budget.defaultLimits, budget.balance, budget.defaultLimits.floatCap))} SUI reserve to draw from</span>
    </label>
    {!available && <p id="enrollment-funding-help" className="text-gray-700">Your fleet budget needs at least {formatToken(budget.defaultLimits.floatCap)} SUI to cover this device’s starting balance. You can register without this funding or <Link className={LINK} href="/budget">fund the fleet budget</Link> first.</p>}
  </div>;
}
