"use client";

import { useFleetSelection } from "@/hooks/useFleetSelection";

import Link from "next/link";
import { isValidSuiAddress, normalizeSuiAddress } from "@mysten/sui/utils";
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
  const { fleet } = useFleetSelection();
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
      return true;
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
      return false;
    }
  }

  return (
    <div className="max-w-3xl space-y-6">
      <PageHeader title="Access policies" subtitle="Choose which devices and accounts can open private software releases. This does not give them permission to publish updates or manage your fleet." />
      <p className="text-xs text-gray-600">
        Grants and revocations are signed transactions; recipients use their own keys. Revocation stops future key
        release once key servers observe it, but cannot recall keys or plaintext already obtained, clear cached keys, or uninstall delivered software. Replacing the encryption key blocks new access to earlier releases. Encrypt and deploy replacement copies of any software devices still need.
      </p>
      <ol className="list-decimal pl-5 text-sm space-y-1">
        <li>Name a policy for the releases you want to protect.</li>
        <li>Grant decryption to activated devices, groups you administer, or an account.</li>
        <li>Choose this policy when uploading a confidential release in <Link className="text-blue-700 underline" href="/firmware">Deploy component</Link>.</li>
      </ol>
      {!POLICIES_ENABLED && <section role="status" className="border rounded p-4 space-y-2">
        <h2 className="font-semibold">Access policies unavailable on this deployment</h2>
        <p className="text-sm">This fleet was created before private-release access controls were available. Connecting a wallet will not add support.
          Existing public OTA releases remain public; creating a policy cannot encrypt them retroactively.</p>
        <p className="text-sm">Ask your fleet administrator to set up a supported private-release configuration.
          Do not replace this fleet registry or use public delivery for confidential content.</p>
        <Link className="text-blue-700 underline text-sm" href="/devices">View this fleet’s devices</Link>
      </section>}
      {!account && <p role="status" className="text-sm">Connect your wallet using the wallet control to view policies you administer.
        People receiving access do not need administrator permissions.</p>}
      {POLICIES_ENABLED && !SEAL_SETTINGS && <section role="status" className="border rounded p-4 text-sm">
        <h2 className="font-semibold">Encryption key servers are not configured</h2>
        <p>You can manage policies, but confidential uploads are unavailable. Ask your administrator to configure
          the encryption service for this app. Provider credentials must never be included in browser code.</p>
      </section>}
      {account && POLICIES_ENABLED && <>
        <form className="border rounded p-4 space-y-3" onSubmit={event => {
          event.preventDefault();
          if (isPending || !name.trim()) return;
          void run(() => buildCreatePolicyTx(name, account.address, fleet), "Policy created").then(ok => { if (ok) setName(""); });
        }}>
          <h2 className="font-semibold">1. Create a named policy</h2>
          <p className="text-sm">Your account will manage this policy. You will choose who receives access after creating it.</p>
          <div className="flex flex-wrap gap-3 items-end">
            <label className="text-sm min-w-0">Policy name
              <input className="block border rounded px-3 py-2 w-full" required maxLength={128} value={name}
                onChange={e => setName(e.target.value)} placeholder="Production sensor releases" />
            </label>
            <button className="border rounded px-3 py-2" disabled={isPending || !name.trim()}>Create policy</button>
          </div>
        </form>
        {policies.isPending && <p role="status">Loading policies you administer…</p>}
        {policies.error && <div role="alert" className="text-sm space-y-2">
          <p>Could not refresh access policies. Previously loaded results may be out of date. {String(policies.error)}</p>
          <button className="border rounded px-3 py-2" disabled={policies.isFetching} onClick={() => void policies.refetch()}>Retry policies</button>
        </div>}
        {!policies.isPending && !policies.error && policies.data?.length === 0 && <p role="status" className="text-sm">
          No policies administered by this wallet. Create your first named policy above, or connect the account that manages an existing policy.
        </p>}
        <p role="status" className="text-sm break-words whitespace-pre-wrap">{isPending ? "Waiting for wallet approval and verified transaction effects…" : message}</p>
        {policies.data?.map(policy => <PolicyCard key={`${account.address}:${policy.objectId}`} policy={policy} pending={isPending} run={run} />)}
      </>}

    </div>
  );
}

function PolicyCard({ policy, pending, run }: {
  policy: AccessPolicy;
  pending: boolean;
  run: (build: () => Transaction, done: string) => Promise<boolean>;
}) {
  const deviceQuery = useDevices();
  const groupQuery = useGroups();
  const devices = deviceQuery.data ?? [];
  const groups = groupQuery.data ?? [];
  const [scope, setScope] = useState<Scope>("device");
  const [subject, setSubject] = useState("");
  const recipients = scope === "device" ? devices.filter(d => d.ownerCapId) : groups;
  const recipientQuery = scope === "device" ? deviceQuery : groupQuery;
  const validSubject = scope === "account" ? isValidSuiAddress(subject) :
    !recipientQuery.isPending && !recipientQuery.error && recipients.some(r => r.objectId === subject);
  const duplicate = (scope === "device" ? policy.devices : scope === "group" ? policy.groups : policy.accounts).includes(isValidSuiAddress(subject) ? normalizeSuiAddress(subject) : subject);
  const label = (s: Scope, id: string) =>
    s === "device" ? devices.find(d => d.objectId === id)?.name ?? id
      : s === "group" ? groups.find(g => g.objectId === id)?.name ?? id : id;
  const entries: [Scope, string[]][] = [["device", policy.devices], ["group", policy.groups], ["account", policy.accounts]];
  return (
    <section aria-label={`Policy ${policy.name}`} className="border border-gray-200 rounded p-4 space-y-4 text-sm min-w-0">
      <h2 className="font-semibold">{policy.name}</h2>
      <details className="text-xs text-gray-600 break-all"><summary>Policy technical details</summary><p>Object ID: {policy.objectId}</p><p>Key epoch {policy.keyEpoch} · code version {policy.version}</p></details>
      <h3 className="font-semibold">2. Manage decryption recipients</h3>
      <p>Device grants require an activated registry key. Group grants follow current membership and delegate recipient selection to the group administrator. Account grants let that address request keys directly.</p>
      {entries.map(([s, ids]) => (
        <div key={s}>
          <p className="text-xs text-gray-500 capitalize">{s}s</p>
          {ids.length === 0 && <p className="text-xs text-gray-400">none</p>}
          {ids.map(id => (
            <div key={id} className="flex flex-wrap items-center gap-2 break-all">
              <span className="text-sm">{label(s, id)}</span>
              <button disabled={pending} aria-label={`Revoke ${s} ${label(s, id)}`} className="border rounded px-3 py-2 text-red-700" onClick={() => run(() => buildRevokeTx(policy, s, id), `Revoked ${s}`)}>Revoke</button>
            </div>
          ))}
        </div>
      ))}
      <form className="space-y-3" onSubmit={event => {
        event.preventDefault();
        if (!validSubject || duplicate || pending) return;
        void run(() => buildGrantTx(policy, scope, subject), `Granted ${scope}`).then(ok => { if (ok) setSubject(""); });
      }}>
        <div className="flex flex-wrap gap-3 items-end">
          <label>Recipient type
            <select className="block border rounded px-3 py-2" value={scope} onChange={e => { setScope(e.target.value as Scope); setSubject(""); }}>
              <option value="device">Device</option>
              <option value="group">Group (you administer)</option>
              <option value="account">Account address</option>
            </select>
          </label>
          {scope !== "account" ? <label className="min-w-0">{scope === "device" ? "Activated device" : "Group"}
            <select aria-label={scope === "device" ? "Activated device" : "Group"} className="block border rounded px-3 py-2 max-w-full" value={subject} onChange={e => setSubject(e.target.value)} disabled={recipientQuery.isPending || !!recipientQuery.error}>
              <option value="">Choose a {scope}…</option>
              {recipients.map(r => <option key={r.objectId} value={r.objectId}>{r.name} ({r.objectId.slice(0, 8)}…)</option>)}
            </select>
          </label> : <label className="min-w-0">Account address
            <input className="block border rounded px-3 py-2 w-full" value={subject} onChange={e => setSubject(e.target.value.trim())} placeholder="0x…" spellCheck={false} />
          </label>}
          <button className="border rounded px-3 py-2" disabled={pending || !validSubject || duplicate}>Grant decryption</button>
        </div>
        {scope === "account" && subject && !validSubject && <p role="status">Enter a complete Sui account address (0x followed by 64 hexadecimal characters).</p>}
        {duplicate && <p role="status">This recipient already has a grant.</p>}
        {scope !== "account" && <>
          {recipientQuery.isPending && <p role="status">Loading {scope} choices…</p>}
          {recipientQuery.error && <div role="alert">Could not load {scope} choices. <button type="button" className="underline" onClick={() => void recipientQuery.refetch()}>Retry recipients</button></div>}
          {!recipientQuery.isPending && !recipientQuery.error && recipients.length === 0 && <p role="status">
            {scope === "device" ? "No activated devices available. Enroll and activate a device in " : "No administered groups available. Create a group and add devices in "}
            <Link className="text-blue-700 underline" href="/devices">{scope === "device" ? "Devices" : "Devices → Device groups"}</Link> first.
          </p>}
        </>}
      </form>
      <button disabled={pending} className="border rounded px-3 py-2 text-amber-800" onClick={() => {
        if (confirm("Replace this encryption key? New requests will no longer be able to open earlier releases. Publish replacement copies of any software devices still need.")) {
          void run(() => buildRotateTx(policy), "Encryption key replaced");
        }
      }}>Replace encryption key</button>
    </section>
  );
}
