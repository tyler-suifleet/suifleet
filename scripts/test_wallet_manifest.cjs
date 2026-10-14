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
const hook = loader({
  "@tanstack/react-query": { useMutation: (options) => options },
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
  const oldService = service.getServiceInfo;
  service.getServiceInfo = async () => { account = { ...goodAccount }; return oldService(); };
  const before = signs; await assert.rejects(mutate({ transaction: {} }), /account changed/); assert.equal(signs, before);
  console.log("PASS: wallet hook network/account checks, stale chain, disconnect/rejection and wallet/node execution failures");
})().catch((error) => { console.error(error); process.exitCode = 1; });
