"use client";

import { useState } from "react";
import { useCurrentAccount } from "@mysten/dapp-kit-react";
import type { Transaction } from "@mysten/sui/transactions";
import { PageHeader } from "@/components/ui";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { useAccessPolicies, POLICIES_ENABLED } from "@/hooks/useAccessPolicies";
import { useDevices } from "@/hooks/useDevices";
import { useGroups } from "@/hooks/useGroups";
import { buildCreatePolicyTx, buildGrantTx, buildRevokeTx, buildRotateTx, type AccessPolicy, type Scope } from "@/lib/access-policy";
import { SEAL_SETTINGS } from "@/lib/seal";

/** Seal access policies (SF-022): who may decrypt confidential releases. */
export default function AccessPage() {
  const account = useCurrentAccount();
  const policies = useAccessPolicies();
  const { mutateAsync: execute, isPending } = useFleetTransaction();
  const [name, setName] = useState("");
  const [message, setMessage] = useState("");

  async function run(build: () => Transaction, done: string) {
    setMessage("");
    try {
      const result = await execute({ transaction: build() });
      setMessage(`${done} (${result.digest})`);
      await policies.refetch();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  }

  if (!account) return <p className="text-sm text-gray-500">Connect your wallet to manage access policies.</p>;
  if (!POLICIES_ENABLED) return <p role="alert">This deployment&apos;s package does not include Seal access policies.</p>;
  return (
    <div className="max-w-3xl space-y-4">
      <PageHeader title="Access policies" subtitle="Who may decrypt confidential releases. Decryption grants no publish, deploy or admin authority." />
      <p className="text-xs text-gray-600">
        Grants and revocations are signed transactions; recipients use their own keys. Revocation stops future key
        release once key servers observe it, but cannot recall keys or plaintext already obtained. Rotating the key
        epoch retires every earlier release: re-encrypt and redeploy what devices still need.
      </p>
      {!SEAL_SETTINGS && <p role="alert" className="text-xs text-red-600">Seal key servers are not configured for this frontend: policies can be managed, but uploads cannot be encrypted.</p>}
      <div className="flex gap-2 items-end">
        <label className="text-sm">New policy name <input value={name} onChange={e => setName(e.target.value)} placeholder="production releases" /></label>
        <button disabled={isPending || !name.trim()} onClick={() => run(() => buildCreatePolicyTx(name, account.address), "Policy created").then(() => setName(""))}>Create policy</button>
      </div>
      {policies.error && <p role="alert">{String(policies.error)}</p>}
      <p role="status" className="text-xs whitespace-pre-wrap">{message}</p>
      {policies.data?.map(policy => <PolicyCard key={policy.objectId} policy={policy} pending={isPending} run={run} />)}
    </div>
  );
}

function PolicyCard({ policy, pending, run }: {
  policy: AccessPolicy;
  pending: boolean;
  run: (build: () => Transaction, done: string) => Promise<void>;
}) {
  const { data: devices = [] } = useDevices();
  const { data: groups = [] } = useGroups();
  const [scope, setScope] = useState<Scope>("device");
  const [subject, setSubject] = useState("");
  const label = (s: Scope, id: string) =>
    s === "device" ? devices.find(d => d.objectId === id)?.name ?? id
      : s === "group" ? groups.find(g => g.objectId === id)?.name ?? id : id;
  const entries: [Scope, string[]][] = [["device", policy.devices], ["group", policy.groups], ["account", policy.accounts]];
  return (
    <div className="border border-gray-200 rounded p-4 space-y-2 text-sm">
      <h2 className="font-semibold">{policy.name}</h2>
      <p className="text-xs font-mono text-gray-500">{policy.objectId} · key epoch {policy.keyEpoch} · code version {policy.version}</p>
      {entries.map(([s, ids]) => (
        <div key={s}>
          <p className="text-xs text-gray-500 capitalize">{s}s</p>
          {ids.length === 0 && <p className="text-xs text-gray-400">none</p>}
          {ids.map(id => (
            <div key={id} className="flex items-center gap-2">
              <span className="font-mono text-xs">{label(s, id)}</span>
              <button disabled={pending} className="text-xs text-red-600" onClick={() => run(() => buildRevokeTx(policy, s, id), `Revoked ${s}`)}>Revoke</button>
            </div>
          ))}
        </div>
      ))}
      <div className="flex gap-2 items-end">
        <select value={scope} onChange={e => { setScope(e.target.value as Scope); setSubject(""); }}>
          <option value="device">Device</option>
          <option value="group">Group (you administer)</option>
          <option value="account">Account address</option>
        </select>
        {scope === "device" && <select value={subject} onChange={e => setSubject(e.target.value)}>
          <option value="">Choose an activated device…</option>
          {devices.filter(d => d.ownerCapId).map(d => <option key={d.objectId} value={d.objectId}>{d.name}</option>)}
        </select>}
        {scope === "group" && <select value={subject} onChange={e => setSubject(e.target.value)}>
          <option value="">Choose a group…</option>
          {groups.map(g => <option key={g.objectId} value={g.objectId}>{g.name}</option>)}
        </select>}
        {scope === "account" && <input value={subject} onChange={e => setSubject(e.target.value)} placeholder="0x…" />}
        <button disabled={pending || !subject} onClick={() => run(() => buildGrantTx(policy, scope, subject), `Granted ${scope}`).then(() => setSubject(""))}>Grant</button>
      </div>
      <button disabled={pending} className="text-xs text-amber-700" onClick={() => {
        if (confirm("Rotate the key epoch? Every release encrypted under this policy so far becomes undecryptable for new requests.")) {
          void run(() => buildRotateTx(policy), "Key epoch rotated");
        }
      }}>Rotate key epoch</button>
    </div>
  );
}
