"use client";
import { useState } from "react";
import { useCurrentClient, useDAppKit } from "@mysten/dapp-kit-react";
import { Transaction } from "@mysten/sui/transactions";
import { fromBase64 } from "@mysten/sui/utils";
import { SECURITY_ACTIONS } from "@/lib/security-policy";
import { PACKAGE_ID } from "@/lib/constants";
import { requireManifest, verifyDeployment } from "@/lib/deployment-manifest";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { addPartial, enrollMultisigTx, exportMultisig, fetchMultisig, multisigKey, multisigPolicyTx, readEnvelope, submitMultisig, type MultisigConfiguration } from "@/lib/multisig-admin";

export function MultisigAdministrator({ customerId = "" }: { customerId?: string }) {
  const client = useCurrentClient(), wallet = useDAppKit(), mutation = useFleetTransaction();
  const [organization, setOrganization] = useState(customerId), [configText, setConfig] = useState('{"keys":[],"weights":[],"threshold":2}');
  const [bundle, setBundle] = useState(""), [partial, setPartial] = useState(""), [action, setAction] = useState(""), [masks, setMasks] = useState("1,1,1,1,1,1,1"), [transaction, setTransaction] = useState(""), [message, setMessage] = useState(""), [busy, setBusy] = useState(false);
  let config: MultisigConfiguration | undefined, address = "", members: string[] = [];
  try { config = JSON.parse(configText); const key = multisigKey(config!); address = key.toSuiAddress(); members = key.getPublicKeys().map(k => `${k.publicKey.toSuiAddress()} (weight ${k.weight})`); } catch { /* incomplete form */ }
  async function run(fn: () => Promise<void>) { setBusy(true); setMessage(""); try { await fn(); } catch(e) { setMessage(e instanceof Error ? e.message : "Multisig operation failed"); } finally { setBusy(false); } }
  async function build(tx: Transaction) { const e = await exportMultisig(client, organization, config!, tx); setBundle(JSON.stringify(e, null, 2)); }
  let preview = "";
  try { if (bundle) preview = JSON.stringify(Transaction.from(fromBase64(readEnvelope(bundle).bytes)).getData(), null, 2); } catch { preview = "Invalid bundle: validate before signing"; }
  return <section aria-label="Multisig administrator" className="space-y-3 rounded border p-4">
    <h3>Multi-signature administration</h3>
    <p>Optional per organization. Register the address using current administrator authority, fund its transaction gas, then enroll using its quorum. Public keys include their Sui scheme byte in base64. Wallet, hardware wallet and passkey members can share public signatures; never paste private keys or recovery phrases.</p>
    <div className="space-y-4 rounded-xl bg-slate-50 p-4"><h4 className="font-semibold text-slate-900">1. Set up signing members</h4>
    <label className="block">Organization object ID<input value={organization} onChange={e => setOrganization(e.target.value)} className="block w-full border"/></label>
    <label className="block">Ordered public keys, weights and threshold (JSON)<textarea value={configText} onChange={e => setConfig(e.target.value)} className="block w-full border"/></label>
    {address && <><p>Derived address: {address}. Threshold: {config!.threshold}.</p><ul>{members.map(m => <li key={m}>{m}</li>)}</ul></>}
    <button disabled={busy} onClick={() => run(async () => { setConfig(JSON.stringify(await fetchMultisig(client, organization), null, 2)); setMessage("Read authenticated on-chain member configuration"); })}>Read enrolled members</button>{" "}
    <button disabled={busy || !config} onClick={() => run(async () => { const tx = new Transaction(); tx.moveCall({ target: `${PACKAGE_ID}::customer::set_credential`, arguments: [tx.object(organization), tx.pure.address(address), tx.pure.bool(true), tx.pure.bool(true)] }); await mutation.mutateAsync({ transaction: tx }); setMessage("Administrator registered; quorum enrollment is still required"); })}>Register address with connected administrator</button>{" "}
    <button disabled={busy || !config} onClick={() => run(async () => build(enrollMultisigTx(organization, config!)))}>Build quorum enrollment</button>
    </div>
    <div className="space-y-4 rounded-xl border border-slate-200 p-4"><h4 className="font-semibold text-slate-900">2. Prepare a policy change</h4>
    <p>Policy masks follow the seven action classes above (1 enables multisig; 0 disables it). Prepare the change, collect signatures and submit, then copy its Action ID from the confirmed transaction events. Build execution with that ID. Changing an active policy requires its current factors; all enabled factors combine. Other enabled mechanisms require their own proofs.</p>
    <p>Action order: {SECURITY_ACTIONS.join("; ")}.</p>
    <label className="block">Seven policy masks<input value={masks} onChange={e => setMasks(e.target.value)} className="block w-full border"/></label>
    <label className="block">Prepared Action ID<input value={action} onChange={e => setAction(e.target.value)} className="block w-full border"/></label>
    <button disabled={busy || !config} onClick={() => run(async () => build(await multisigPolicyTx(client, organization, config!, masks.split(",").map(Number))))}>Build policy preparation</button>{" "}
    <button disabled={busy || !config || !action} onClick={() => run(async () => build(await multisigPolicyTx(client, organization, config!, masks.split(",").map(Number), action)))}>Build policy execution</button>
    <details><summary>Other protected transactions</summary><p>Paste a console/SDK Transaction JSON to resolve and export its exact bytes. Include the operation-specific action wrapper and approvals. Every signer must review all commands, recipients and gas independently.</p><textarea aria-label="Transaction JSON" value={transaction} onChange={e => setTransaction(e.target.value)} className="block w-full border"/><button disabled={busy || !config} onClick={() => run(async () => build(Transaction.from(transaction)))}>Build transaction bundle</button></details>
    </div>
    <div className="space-y-4 rounded-xl border border-slate-200 p-4"><h4 className="font-semibold text-slate-900">3. Review and collect signatures</h4>
    <label className="block">Export/import public transaction bundle<textarea value={bundle} onChange={e => setBundle(e.target.value)} className="block w-full border" rows={7}/></label>
    <details><summary>Review exact transaction commands and gas</summary><pre className="overflow-auto">{preview}</pre></details>
    <p>Bundle labels are untrusted. Verify the deployment, exact transaction commands, organization, recipients and costs before signing. A signature authorizes these bytes. Share this public bundle with the other members, who can sign through their wallet or import a signature from an external hardware-wallet tool. Slush and extension wallets must support signTransaction without execution.</p>
    <button disabled={busy || !bundle} onClick={() => run(async () => {
      const e = readEnvelope(bundle), manifest = requireManifest(), account = wallet.stores.$connection.get().account;
      if (!account || !account.chains.includes(`sui:${manifest.network}`) || wallet.stores.$currentNetwork.get() !== manifest.network) throw Error("Connect a member wallet on the deployment network");
      if (!multisigKey(e.config).getPublicKeys().some(k => k.publicKey.toSuiAddress() === account.address)) throw Error("Connected wallet is not a member");
      await verifyDeployment(client);
      if (wallet.stores.$connection.get().account !== account || wallet.stores.$currentNetwork.get() !== manifest.network) throw Error("Wallet changed; retry");
      const signed = await wallet.signTransaction({ transaction: Transaction.from(fromBase64(e.bytes)), account, network: manifest.network });
      if (wallet.stores.$connection.get().account !== account || wallet.stores.$currentNetwork.get() !== manifest.network) throw Error("Wallet changed during signing; retry");
      if (signed.bytes !== e.bytes) throw Error("Wallet changed the transaction bytes");
      const signature = signed.signature; setPartial(signature); setBundle(JSON.stringify(await addPartial(e, signature), null, 2));
    })}>Sign exact bytes with member wallet</button>
    <label className="block">Export/import one partial signature<textarea value={partial} onChange={e => setPartial(e.target.value)} className="block w-full border"/></label>
    <button disabled={busy || !bundle || !partial} onClick={() => run(async () => setBundle(JSON.stringify(await addPartial(readEnvelope(bundle), partial.trim()), null, 2)))}>Verify and add partial signature</button>{" "}
    <button disabled={busy || !bundle} onClick={() => run(async () => { setMessage(`Confirmed ${await submitMultisig(client, readEnvelope(bundle))}. Inspect its events for the Action ID. Reconcile this digest before any retry.`); })}>Verify quorum and submit</button>
    </div>
    <p className="rounded-xl border border-amber-200 bg-amber-50 p-4">Recovery: a surviving quorum authorizes a policy change under the current requirements, registers and proves a replacement quorum, then restores protection. Loss of quorum without preauthorized recovery can permanently lock administration. No support reset exists. Ambiguous submissions require digest reconciliation before retry.</p>
    {message && <p role="status">{message}</p>}
  </section>;
}
