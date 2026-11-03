"use client";
import { PasskeySettings } from "./PasskeySettings";
import { passkeySupported } from "@/lib/passkey-factor";
import { SecondPersonSetup } from "./SecondPersonSetup";
import { SecondPersonApprovals } from "./SecondPersonApprovals";
import { MultisigAdministrator } from "./MultisigAdministrator";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { AVAILABLE_SECURITY_FACTORS, SECURITY_ACTIONS, SECURITY_FACTORS, buildSecurityPolicyTx, fetchOrganizationSecurity, securitySupported } from "@/lib/security-policy";

export function OrganizationSecurity() {
  const selection = useFleetSelection(), client = useCurrentClient(), mutation = useFleetTransaction();
  const fleet = selection.fleet;
  const [error, setError] = useState("");
  const policy = useQuery({ queryKey: ["organization-security", fleet?.customerId], enabled: !!fleet && securitySupported(), queryFn: () => fetchOrganizationSecurity(client, fleet!.customerId) });
  if (!fleet) return <><p>Select an organization to view its security policy.</p><MultisigAdministrator/></>;
  return <section className="space-y-4" aria-label="Organization security">
    <p>These settings apply to every fleet in {fleet.customerName}. Enabled requirements combine: every selected protection must pass. All four default off.</p>
    {!securitySupported() && <p>The connected deployment does not include the security framework. This organization is unprotected.</p>}
    {policy.isPending && securitySupported() && <p>Reading the on-chain policy…</p>}
    {policy.error && <p role="alert">{policy.error.message}</p>}
    {policy.isSuccess && !policy.data && <p>This organization has legacy authority. It has not migrated to the security framework. A missing policy does not mean protections are configured off.</p>}
    {policy.data && <><p>Verified policy generation: {policy.data.generation.toString()}.</p><ul>{SECURITY_ACTIONS.map((name, i) => <li key={name}>{name}: {policy.data!.requirements[i] === 0 ? "All protections off" : SECURITY_FACTORS.filter(f => (policy.data!.requirements[i] & f.bit) !== 0).map(f => f.name).join(" + ")}</li>)}</ul></>}
    <PasskeySettings />
    {policy.data && fleet.administrator && passkeySupported() && <button disabled={mutation.isPending} onClick={async () => { try { setError(""); const values = policy.data!.requirements.map(v => policy.data!.requirements.some(r => (r & 2) !== 0) ? v & ~2 : v | 2); await mutation.mutateAsync({ transaction: buildSecurityPolicyTx(fleet.customerId, values) }); await policy.refetch(); } catch (e) { setError(e instanceof Error ? e.message : "Passkey policy update failed"); } }}>Toggle passkey protection for all actions</button>}
    {SECURITY_FACTORS.map(f => <label key={f.bit} className="block rounded border p-3"><input type="checkbox" disabled checked={!!policy.data?.requirements.some(v => (v & f.bit) !== 0)} readOnly/> <strong>{f.name}</strong><p>{f.description}</p>{(AVAILABLE_SECURITY_FACTORS & f.bit) === 0 && <p>Not available yet: requires {f.ticket} and verified enrollment. It cannot be enabled by this framework alone.</p>}</label>)}
    <SecondPersonSetup key={fleet.customerId} />
    <SecondPersonApprovals />
    <p>Protected organizations keep release publishers and fleet capabilities in organization custody. Storage withdrawals use trusted wrappers that bind the amount and recipient. Generic external storage or data capability loans are unavailable while protection is enabled; other operations require a reviewed adapter. Whole-organization handover preserves custody and requires recipient confirmation.</p>
    <p>Disabling or weakening protection requires the current factors. Losing a required factor can permanently lock administration; there is no email or support reset. Enroll independent recovery access before activation.</p>
    {policy.data && fleet.administrator && <button disabled={mutation.isPending || policy.data.requirements.some(v => v !== 0)} onClick={async () => { try { setError(""); await mutation.mutateAsync({ transaction: buildSecurityPolicyTx(fleet.customerId, policy.data!.requirements) }); await policy.refetch(); } catch (e) { setError(e instanceof Error ? e.message : "Policy update failed"); } }}>Save current all-off policy</button>}
    <MultisigAdministrator key={fleet.customerId} customerId={fleet.customerId}/>
    {error && <p role="alert">{error}</p>}
  </section>;
}
