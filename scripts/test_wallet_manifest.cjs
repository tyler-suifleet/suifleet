const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { loader, manifestClient } = require("./frontend_test_utils.cjs");
const fixture = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../manifests/fixture.localnet.json"), "utf8"));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(fixture);
let execution = "success", signingError, signs = 0, confirmation = "success", account = { address: "0x1", chains: [`sui:${fixture.network}`] }, network = fixture.network;
const client = manifestClient(fixture);
function result(success) { return success ? { $kind: "Transaction", Transaction: { digest: "test-digest", status: { success: true } } } : { $kind: "FailedTransaction", FailedTransaction: { digest: "test-digest", status: { success: false, error: { message: "Move abort" } } } }; }
client.waitForTransaction = async () => result(confirmation === "success");
const wallet = {
  stores: { $connection: { get: () => ({ account }) }, $currentNetwork: { get: () => network } },
  signAndExecuteTransaction: async (args) => {
    signs++; assert.equal(args.network, fixture.network); assert.equal(args.account, account);
    if (signingError) throw new Error(signingError);
    return result(execution === "success");
  },
};
// Compile the real TSX provider too; loading the hook below uses a hook fixture.
assert.equal(typeof loader()("hooks/useFleetSelection.tsx").FleetSelectionProvider, "function");
const hook = loader({
  "react": { useRef: (current) => ({ current }) },
  "./useFleetSelection": { useFleetSelection: () => ({ supported: false }) },
  "@tanstack/react-query": { useMutation: (options) => options, useQueryClient: () => ({ invalidateQueries: async () => {} }) },
  "@mysten/dapp-kit-react": { useCurrentClient: () => client, useDAppKit: () => wallet },
})("hooks/useFleetTransaction.ts");
(async () => {
  const mutate = hook.useFleetTransaction().mutationFn;
  assert.equal((await mutate({ transaction: {} })).digest, "test-digest");
  assert.equal(signs, 1);
  const goodAccount = account;
  account = null; await assert.rejects(mutate({ transaction: {} }), /Connect a wallet/); assert.equal(signs, 1);
  account = { ...goodAccount, chains: ["sui:mainnet"] }; await assert.rejects(mutate({ transaction: {} }), /does not support/); assert.equal(signs, 1);
  account = goodAccount; network = "mainnet"; await assert.rejects(mutate({ transaction: {} }), /network/); assert.equal(signs, 1); network = fixture.network;
  const service = client.ledgerService;
  client.ledgerService = manifestClient({ ...fixture, chain_identifier: "deadbeef" }).ledgerService;
  await assert.rejects(mutate({ transaction: {} }), /chain identifier/); assert.equal(signs, 1); client.ledgerService = service;
  signingError = "Wallet disconnected"; await assert.rejects(mutate({ transaction: {} }), /Wallet disconnected/);
  signingError = "User rejected signing"; await assert.rejects(mutate({ transaction: {} }), /User rejected signing/);
  signingError = undefined; execution = "failure"; await assert.rejects(mutate({ transaction: {} }), /Move abort/);
  execution = "success"; confirmation = "failure"; await assert.rejects(mutate({ transaction: {} }), /Move abort/);
  confirmation = "success";
  // SF-160: retain successful wallet evidence before node confirmation and
  // distinguish definite refusal from an ambiguous transport interruption.
  let digest, refused = false;
  const callbacks = { onSubmitted: value => { digest = value; }, onDefiniteFailure: () => { refused = true; } };
  confirmation = "failure";
  await assert.rejects(mutate({ transaction: {}, ...callbacks }), /Move abort/);
  assert.equal(digest, "test-digest"); assert.equal(refused, false);
  confirmation = "success"; digest = undefined;
  signingError = "Connection lost after submission";
  await assert.rejects(mutate({ transaction: {}, ...callbacks }), /Connection lost/);
  assert.equal(digest, undefined); assert.equal(refused, false);
  signingError = "User rejected the request";
  await assert.rejects(mutate({ transaction: {}, ...callbacks }), /User rejected/);
  assert.equal(refused, true); refused = false; signingError = undefined;
  execution = "failure";
  await assert.rejects(mutate({ transaction: {}, ...callbacks }), /Move abort/);
  assert.equal(refused, true); assert.equal(digest, undefined); execution = "success";
  const oldService = service.getServiceInfo;
  service.getServiceInfo = async () => { account = { ...goodAccount }; return oldService(); };
  assert.equal((await mutate({ transaction: {} })).digest, "test-digest", "same account rebuilt by wallet is valid");
  account = {...goodAccount, chains:[`sui:${fixture.network}`, "sui:mainnet"]};
  service.getServiceInfo = async () => { account = {...account, chains:[...account.chains].reverse()}; return oldService(); };
  assert.equal((await mutate({ transaction: {} })).digest, "test-digest", "chain order is not an account change");
  for (const [change, expected] of [
    [() => {account={...account,address:"0x2"};}, /account changed/],
    [() => {account.address="0x2";}, /account changed/],
    [() => {account.chains.push("sui:mainnet");}, /account changed/],
    [() => {account=null;}, /account changed/],
    [() => {network="mainnet";}, /network changed/],
  ]) {
    account = {...goodAccount, chains:[...goodAccount.chains]}; network=fixture.network;
    service.getServiceInfo = async () => {change();return oldService();};
    const before=signs; await assert.rejects(mutate({transaction:{}}), expected); assert.equal(signs,before);
  }
  console.log("PASS: wallet hook network/account checks, stale chain, disconnect/rejection and wallet/node execution failures");
})().catch((error) => { console.error(error); process.exitCode = 1; });
