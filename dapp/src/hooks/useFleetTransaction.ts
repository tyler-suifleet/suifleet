"use client";

import { fetchOrganizationSecurity } from "@/lib/security-policy";
import { approvalDecisionsOnly, assertSponsorSafe, enrolledSigner, withSponsorFunding } from "@/lib/passkey-factor";
import { fromBase64, toBase64 } from "@mysten/sui/utils";
import { Transaction as SuiTransaction } from "@mysten/sui/transactions";
import { checkDeviceCapacity } from "@/lib/customer-entitlements";
import { getSession, isEnokiWallet } from "@mysten/enoki";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCurrentClient, useDAppKit } from "@mysten/dapp-kit-react";
import { useRef } from "react";
import { useFleetSelection } from "./useFleetSelection";
import { fetchCustomerFleets, selectionKey, withManagedBudgetCap } from "@/lib/customer-fleets";
import { fetchManagedBudget } from "@/lib/fleet-budget";
import { verifySupportDiagnostics } from "@/lib/fleet-support";
import type { FleetDataConfig } from "@/lib/fleet-data";
import type { Transaction } from "@mysten/sui/transactions";
import { requireManifest, verifyDeployment } from "@/lib/deployment-manifest";
import { FleetTransactionError, isWalletRejection } from "@/lib/tx-feedback";

import { readAccount, depositTx, coin, units, validatePayment, type Payment } from "@/lib/subscription";

export type TransactionPhase = "validating" | "wallet" | "confirming";
type FleetTransactionInput = { transaction: Transaction; subscriptionDeposit?: Payment; onProgress?: (phase: TransactionPhase) => void; onSubmitted?: (digest: string) => void; onDefiniteFailure?: () => void; support?: { config: FleetDataConfig; deviceId: string } };

export function useFleetTransaction() {
  const selection = useFleetSelection();
  const cache = useQueryClient();
  const latest = useRef(selection); latest.current = selection;
  const client = useCurrentClient();
  const wallet = useDAppKit();
  async function execute({ transaction, onProgress, onSubmitted, onDefiniteFailure, support, subscriptionDeposit }: FleetTransactionInput, progress: { sent: boolean; digest?: string }) {
      const captured = selection;
      onProgress?.("validating");
      const manifest = requireManifest();
      const account = wallet.stores.$connection.get().account;
      if (!account) throw new Error("Connect a wallet before submitting a transaction");
      const address = account.address;
      const chains = JSON.stringify([...new Set(account.chains)].sort());
      if (!account.chains.includes(`sui:${manifest.network}`)) throw new Error(`Wallet account does not support sui:${manifest.network}`);
      if (wallet.stores.$currentNetwork.get() !== manifest.network) throw new Error("Wallet network does not match the deployment manifest");
      await verifyDeployment(client);
      if (subscriptionDeposit) {
        validatePayment(subscriptionDeposit);
        const target = await readAccount(client, subscriptionDeposit.account, subscriptionDeposit.symbol, subscriptionDeposit.customer);
        const balance = await client.getBalance({ owner: address, coinType: coin(subscriptionDeposit.symbol).coin_type });
        if (BigInt(balance.balance.balance) < units(subscriptionDeposit.amount)) throw Error("Insufficient funds for subscription deposit; gas also requires SUI");
        // Rebuild the sole permissionless action from fresh verified state.
        transaction = depositTx(target, subscriptionDeposit.symbol, subscriptionDeposit.amount);
      }
      if (!subscriptionDeposit && captured.supported && captured.fleet) {
        const fleets = await fetchCustomerFleets(client, address);
        const current = fleets.find(f => f.id === captured.fleet!.id && f.customerId === captured.fleet!.customerId);
        if (!current) throw Error("Fleet access was revoked");
        if (support) await verifySupportDiagnostics(client, current, support.config, support.deviceId, address, transaction);
        else {
          if (!current.administrator) throw Error("Fleet access was revoked or is read-only");
          const budget = await fetchManagedBudget(client, current.budgetId);
          transaction = withManagedBudgetCap(transaction, current, budget.capId);
        }
      }
      await checkDeviceCapacity(client, transaction);
      const connectedWallet = wallet.stores.$connection.get().wallet;
      if (connectedWallet && isEnokiWallet(connectedWallet)) {
        const session = await getSession(connectedWallet);
        const { response: info } = await client.ledgerService.getServiceInfo({});
        if (!session || !session.jwt || !Number.isFinite(session.expiresAt) || Date.now() >= session.expiresAt ||
            !Number.isSafeInteger(session.maxEpoch) || info.epoch === undefined || info.epoch > BigInt(session.maxEpoch)) {
          throw new Error("Google sign-in session expired or could not be verified. Disconnect and sign in again before retrying.");
        }
      }
      // Pass the selected account explicitly; never silently sign with an
      // account selected while the asynchronous network proof was in flight.
      const currentAccount = wallet.stores.$connection.get().account;
      if (!currentAccount || currentAccount.address !== address || JSON.stringify([...new Set(currentAccount.chains)].sort()) !== chains) throw new Error("Wallet account changed; retry the transaction");
      if (wallet.stores.$currentNetwork.get() !== manifest.network) throw new Error("Wallet network changed; retry the transaction");
      if (!subscriptionDeposit && captured.supported && (latest.current.identity !== captured.identity || (captured.fleet && localStorage.getItem(selectionKey(address)) && localStorage.getItem(selectionKey(address)) !== captured.fleet.id))) throw Error("Fleet selection changed; retry the transaction");
      onProgress?.("wallet");
      // Permissionless subscription deposits use only the payer wallet, even
      // when an unrelated selected organization requires a passkey.
      const policy = !subscriptionDeposit && captured.fleet && await fetchOrganizationSecurity(client, captured.fleet.customerId);
      // Plain wallet coin funding has no organization Move operation and must
      // remain wallet-paid so an exhausted passkey can be topped up. Actual
      // organization authority is always enforced by Move, independently.
      // Independent approval decisions need only administrator authentication;
      // executing the approved action still requires its configured factors.
      const requiresPasskey = !approvalDecisionsOnly(transaction) && policy && policy.requirements.some(v => (v & 2) !== 0) && transaction.getData().commands.some(command => !!command.MoveCall);
      const submit = async () => {
        // From here the wallet may submit; a later transport error is ambiguous.
        if (!requiresPasskey || !captured.fleet) { progress.sent = true; return wallet.signAndExecuteTransaction({ transaction, account: currentAccount, network: manifest.network }); }
        transaction = withSponsorFunding(transaction);
        const signer = await enrolledSigner(client, captured.fleet.customerId, address);
        transaction.setSender(address); transaction.setGasOwner(signer.toSuiAddress());
        const bytes = await transaction.build({ client });
        assertSponsorSafe(SuiTransaction.from(bytes));
        const signed = await wallet.signTransaction({ transaction: SuiTransaction.from(bytes), account: currentAccount, network: manifest.network });
        if (signed.bytes !== toBase64(bytes)) throw Error("Wallet changed the transaction; passkey signing refused");
        const passkey = await signer.signTransaction(bytes);
        if (passkey.bytes !== signed.bytes) throw Error("Passkey transaction mismatch");
        const now = wallet.stores.$connection.get().account;
        if (!now || now.address !== address || JSON.stringify([...new Set(now.chains)].sort()) !== chains || wallet.stores.$currentNetwork.get() !== manifest.network || latest.current.identity !== captured.identity) throw Error("Wallet, network or fleet changed while signing");
        await verifyDeployment(client);
        if (wallet.stores.$connection.get().account?.address !== address || wallet.stores.$currentNetwork.get() !== manifest.network || latest.current.identity !== captured.identity || (localStorage.getItem(selectionKey(address)) && localStorage.getItem(selectionKey(address)) !== captured.fleet.id)) throw Error("Wallet, network or fleet changed before submission");
        progress.sent = true;
        return client.executeTransaction({ transaction: fromBase64(signed.bytes), signatures: [signed.signature, passkey.signature], include: { effects: true, events: true } });
      };
      const result = await submit().catch((error: unknown) => {
        // A transport error can hide an executed transaction. Only explicit
        // wallet rejection, or a failure before anything was handed over for
        // execution (passkey preparation), permits the caller to retry.
        if (isWalletRejection(error)) { onDefiniteFailure?.(); throw new FleetTransactionError("rejected", error); }
        if (!progress.sent) { onDefiniteFailure?.(); throw new FleetTransactionError("not-submitted", error); }
        throw new FleetTransactionError("uncertain", error);
      });
      if (result.FailedTransaction || !result.Transaction.status.success) {
        onDefiniteFailure?.();
        throw new FleetTransactionError("failed", new Error(result.FailedTransaction?.status.error?.message ?? "Transaction execution was not successful"), result.FailedTransaction?.digest ?? result.Transaction?.digest);
      }
      // Confirm against the verified fleet node, independently of wallet effects.
      onProgress?.("confirming");
      progress.digest = result.Transaction.digest;
      onSubmitted?.(result.Transaction.digest);
      const confirmed = await client.waitForTransaction({ digest: result.Transaction.digest, include: { effects: true, events: true } });
      if (confirmed.FailedTransaction || !confirmed.Transaction.status.success) {
        // The wallet reported success but the verified node did not: treat the
        // outcome as unconfirmed rather than safe to repeat.
        throw new FleetTransactionError("uncertain", new Error(confirmed.FailedTransaction?.status.error?.message ?? "Transaction execution was not successful"), result.Transaction.digest);
      }
      void cache.invalidateQueries({ queryKey: ["customer-fleets"] });
      // Events from the verified node's confirmed effects, not the wallet's.
      return { digest: confirmed.Transaction.digest, events: confirmed.Transaction.events ?? [] };
  }
  return useMutation({
    // Every failure is a FleetTransactionError whose stage tells callers and
    // customers whether anything may have reached the chain (SF-277).
    mutationFn: async (input: FleetTransactionInput) => {
      const progress = { sent: false, digest: undefined as string | undefined };
      try { return await execute(input, progress); }
      catch (error) {
        if (error instanceof FleetTransactionError) throw error;
        throw new FleetTransactionError(progress.sent ? "uncertain" : "not-submitted", error, progress.digest);
      }
    },
  });
}
