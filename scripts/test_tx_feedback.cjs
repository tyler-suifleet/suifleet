// SF-277: the real fleet transaction hook classifies where a transaction
// stopped, and customer guidance never claims success or invites an unsafe
// retry. Wallet, chain and fleet dependencies are scripted stand-ins; the hook,
// outcome classifier and credential-recovery planner are the real modules.
const assert = require("node:assert/strict");
const { loader } = require("./frontend_test_utils.cjs");

const id = (n) => "0x" + n.toString(16).padStart(64, "0");
const manifest = { network: "localnet" };
let wallet, client, mutation;
const account = { address: id(10), chains: ["sui:localnet"] };
const stubs = {
  react: { useRef: (value) => ({ current: value }) },
  "@tanstack/react-query": { useMutation: (options) => (mutation = options), useQueryClient: () => ({ invalidateQueries: async () => {} }) },
  "@mysten/dapp-kit-react": { useCurrentClient: () => client, useDAppKit: () => wallet },
  "@mysten/enoki": { getSession: async () => null, isEnokiWallet: () => false },
  "@/lib/security-policy": { fetchOrganizationSecurity: async () => null },
  "@/lib/passkey-factor": { approvalDecisionsOnly: () => false, assertSponsorSafe() {}, enrolledSigner: async () => { throw Error("unused"); }, withSponsorFunding: (t) => t },
  "@/lib/customer-entitlements": { checkDeviceCapacity: async () => {} },
  "./useFleetSelection": { useFleetSelection: () => ({ supported: false, identity: "legacy" }) },
  "@/lib/customer-fleets": { fetchCustomerFleets: async () => [], selectionKey: () => "k", withManagedBudgetCap: (t) => t },
  "@/lib/fleet-budget": { fetchManagedBudget: async () => ({}) },
  "@/lib/fleet-support": { verifySupportDiagnostics: async () => {} },
  "@/lib/deployment-manifest": { requireManifest: () => manifest, verifyDeployment: async () => {} },
  "@/lib/subscription": {},
};
const load = loader(stubs);
const feedback = load("lib/tx-feedback.ts");
stubs["@/lib/tx-feedback"] = feedback;
const { useFleetTransaction } = load("hooks/useFleetTransaction.ts");
const recovery = load("lib/credential-recovery.ts");

function setup({ network = "localnet", execute, wait } = {}) {
  const calls = { execute: 0 };
  wallet = {
    stores: { $connection: { get: () => ({ account, wallet: { name: "fixture" } }) }, $currentNetwork: { get: () => network } },
    signAndExecuteTransaction: async () => { calls.execute++; return execute(); },
  };
  client = { waitForTransaction: wait ?? (async ({ digest }) => ({ Transaction: { digest, status: { success: true }, events: [] } })) };
  useFleetTransaction();
  return calls;
}
async function run(options) {
  const calls = setup(options);
  const seen = { definite: 0, submitted: [], phases: [] };
  const input = { transaction: { getData: () => ({ commands: [] }) }, onDefiniteFailure: () => seen.definite++, onSubmitted: (d) => seen.submitted.push(d), onProgress: (p) => seen.phases.push(p) };
  try { return { result: await mutation.mutationFn(input), calls, seen }; }
  catch (error) { return { error, calls, seen }; }
}

(async () => {
  // Rejected before anything was sent: retry is safe and the caller may reset.
  let r = await run({ execute: async () => { throw Object.assign(new Error("User rejected the request"), { code: 4001 }); } });
  assert.ok(r.error instanceof feedback.FleetTransactionError);
  assert.equal(r.error.stage, "rejected"); assert.equal(r.seen.definite, 1);
  let f = feedback.describeTransactionFailure(r.error, "Device registration");
  assert.equal(f.retry, "safe"); assert.match(f.title, /cancelled in your wallet/); assert.match(f.next, /Nothing was submitted or charged/);

  // Wrong network: refused before the wallet is asked; nothing sent.
  r = await run({ network: "testnet", execute: async () => { throw Error("must not sign"); } });
  assert.equal(r.calls.execute, 0); assert.equal(r.error.stage, "not-submitted");
  f = feedback.describeTransactionFailure(r.error, "Device registration");
  assert.equal(f.kind, "wrong-network"); assert.equal(f.retry, "fix-first"); assert.match(f.next, /Switch your wallet/);
  assert.match(f.details, /Wallet network does not match/);

  // Transport failure after handing the transaction to the wallet: unknown
  // outcome. The caller must not reset its submitted marker.
  r = await run({ execute: async () => { throw new TypeError("Failed to fetch"); } });
  assert.equal(r.error.stage, "uncertain"); assert.equal(r.seen.definite, 0);
  f = feedback.describeTransactionFailure(r.error, "Device registration");
  assert.equal(f.retry, "check-first"); assert.match(f.title, /may have been submitted/); assert.match(f.next, /Do not try again yet/);
  assert.doesNotMatch(f.title + f.next, /success|completed|done/i);

  // A wallet error that mentions funds is still unproven once sent.
  r = await run({ execute: async () => { throw Error("Insufficient gas balance"); } });
  assert.equal(r.error.stage, "uncertain");
  f = feedback.describeTransactionFailure(r.error);
  assert.equal(f.kind, "insufficient-funds"); assert.equal(f.retry, "check-first");

  // Executed failure: definite, with its digest, and the action did not apply.
  r = await run({ execute: async () => ({ FailedTransaction: { digest: "FAILED1", status: { error: { message: "MoveAbort in 1st command, abort code: 3" } } } }) });
  assert.equal(r.error.stage, "failed"); assert.equal(r.error.digest, "FAILED1"); assert.equal(r.seen.definite, 1);
  f = feedback.describeTransactionFailure(r.error, "Group creation");
  assert.equal(f.kind, "contract"); assert.match(f.title, /failed on chain and did not take effect/); assert.match(f.next, /fee .* may still be charged/);
  assert.equal(f.digest, "FAILED1");

  // Executed per the wallet, then node confirmation is unavailable: unknown, keep digest.
  r = await run({ execute: async () => ({ Transaction: { digest: "D1", status: { success: true } } }), wait: async () => { throw Error("deadline exceeded"); } });
  assert.equal(r.error.stage, "uncertain"); assert.equal(r.error.digest, "D1"); assert.deepEqual(r.seen.submitted, ["D1"]); assert.equal(r.seen.definite, 0);
  f = feedback.describeTransactionFailure(r.error);
  assert.match(f.next, /transaction below/); assert.equal(f.digest, "D1");

  // Wallet says success but the verified node reports failure: never safe to repeat.
  r = await run({ execute: async () => ({ Transaction: { digest: "D2", status: { success: true } } }), wait: async () => ({ FailedTransaction: { status: { error: { message: "MoveAbort" } } } }) });
  assert.equal(r.error.stage, "uncertain"); assert.equal(r.seen.definite, 0);

  // Success returns verified events and reports no failure.
  r = await run({ execute: async () => ({ Transaction: { digest: "OK", status: { success: true } } }) });
  assert.equal(r.error, undefined); assert.equal(r.result.digest, "OK"); assert.deepEqual(r.seen.phases, ["validating", "wallet", "confirming"]);

  // Errors raised outside the hook (builders, preflight reads) never reached a wallet.
  f = feedback.describeTransactionFailure(new Error("Fleet access was revoked"), "Moving the device");
  assert.equal(f.stage, "not-submitted"); assert.equal(f.kind, "access"); assert.match(f.next, /administrator/);
  f = feedback.describeTransactionFailure("plain", "X");
  assert.equal(f.details, "plain"); assert.equal(f.retry, "safe");

  // Lost-credential plan: revoke only unused live claims; never reissue enrolled devices.
  const view = (serial, status) => ({ entry: { serial, alias: serial, claim: id(serial.length + serial.charCodeAt(serial.length - 1)) }, status });
  const views = [view("SN-1", "awaiting-device"), view("SN-2", "complete"), view("SN-3", "unregistered"), view("SN-4", "confirming"), view("SN-5", "unfunded"), view("SN-6", "conflict"), view("SN-7", "revoked")];
  assert.deepEqual(recovery.parseSerials(" SN-1, sn-2;SN-1\nSN-9 "), ["SN-1", "sn-2", "SN-9"]);
  const plan = recovery.planLostCredentials(views, recovery.parseSerials("SN-1 sn-2 SN-3 SN-4 SN-5 SN-6 SN-7 SN-9"));
  assert.deepEqual(plan.revoke.map((v) => v.entry.serial), ["SN-1", "SN-5"]);
  assert.deepEqual(plan.enrolled.map((v) => v.entry.serial), ["SN-2"]);
  assert.deepEqual(plan.release.map((v) => v.entry.serial), ["SN-3", "SN-7"]);
  assert.deepEqual(plan.wait.map((v) => v.entry.serial), ["SN-4"]);
  assert.deepEqual(plan.blocked.map((v) => v.entry.serial), ["SN-6"]);
  assert.deepEqual(plan.unknown, ["SN-9"]);
  console.log("PASS: transaction stages (rejected, not submitted, executed failure, unconfirmed), definite-failure callbacks, customer guidance without invented success, and lost-credential revoke/reissue planning");
})().catch((error) => { console.error(error); process.exitCode = 1; });
