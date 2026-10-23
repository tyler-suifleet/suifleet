// SF-132 offline regressions; real SDK commands, controlled network boundary.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { loader } = require('./frontend_test_utils.cjs');
const manifest = JSON.parse(fs.readFileSync('manifests/fixture.localnet.json'));
const abi = JSON.parse(fs.readFileSync('interfaces/fleet-v1.json'));
for (const name of abi.optional_type_origins.fleet_budget) manifest.type_origins[name] = manifest.call_package_id;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
const load = loader(), { buildEnrollDevicesTx } = load('lib/fleet-budget.ts');
const { enrollBudgetDevices, prepareEnrollment, enrollmentReads } = load('lib/budget-enrollment.ts');
const id = n => '0x' + n.toString(16).padStart(64, '0');
const devices = Array.from({ length: 1000 }, (_, i) => ({ objectId: id(i + 10), ownerCapId: id(i + 2000) }));
const budget = { id: id(1), capId: id(2), balance: 1000000n, defaultLimits: { floatCap: 10n, periodAllowance: 20n } };
const batch = devices.slice(0, 200).map(d => ({ ...d, floatMist: 10n, reserveMist: 20n }));
const tx = buildEnrollDevicesTx(budget.id, budget.capId, batch);
assert.equal(tx.getData().commands.length, 200);
assert.equal(tx.getData().inputs.filter(i => i.UnresolvedObject || i.Object).length, 204);
assert.throws(() => buildEnrollDevicesTx(budget.id, budget.capId, []), /between/);
assert.throws(() => buildEnrollDevicesTx(budget.id, budget.capId, [...batch, batch[0]]), /between/);
assert.throws(() => buildEnrollDevicesTx(budget.id, budget.capId, [batch[0], batch[0]]), /Duplicate/);
assert.throws(() => buildEnrollDevicesTx(budget.id, budget.capId, [{ ...batch[0], reserveMist: -1n }]), /negative/);
(async () => {
  const { device_registry_IdentityBcs } = load('lib/fleet-protocol.ts');
  const { ObjectError } = require('./frontend_test_utils.cjs').appRequire('@mysten/sui/client');
  let revoked = false, ownerCap = devices[0].ownerCapId, missing = false, missingCustody = false, readFailure = false;
  const reader = enrollmentReads({ getDynamicField: async ({ name }) => {
    if (readFailure) throw Error('registry unavailable');
    if (missing || (missingCustody && name.type.includes('Wrapper'))) throw new ObjectError('notFound', 'missing', { reason: 'notFound' });
    if (name.type.includes('Wrapper')) return {};
    return { dynamicField: { value: { type: `${manifest.type_origins['device_registry::Identity']}::device_registry::Identity`,
      bcs: device_registry_IdentityBcs.serialize({ signer: id(99), revoked, owner_cap: ownerCap }).toBytes() } } };
  } }, id(1), budget.id);
  assert.equal(await reader.ineligibleReason(devices[0]), undefined);
  revoked = true; assert.match(await reader.ineligibleReason(devices[0]), /revoked/);
  revoked = false; ownerCap = id(9999); assert.match(await reader.ineligibleReason(devices[0]), /cap changed/);
  missing = true; assert.match(await reader.ineligibleReason(devices[0]), /No active/);
  missing = false; ownerCap = devices[0].ownerCapId; missingCustody = true;
  assert.match(await reader.ineligibleReason(devices[0]), /custody/);
  missingCustody = false; readFailure = true;
  await assert.rejects(reader.ineligibleReason(devices[0]), /registry unavailable/);
  const enrolled = new Set(), approvals = [], progress = [];
  let fail = true, uncertain = false;
  const options = {
    devices, ineligibleReason: async () => undefined, readBudget: async () => budget,
    isEnrolled: async d => enrolled.has(d.objectId),
    prepare: async tx => { assert(tx.getData().commands.length <= 200); return '10000000'; },
    execute: async tx => {
      if (fail && approvals.length === 1) throw Error('wallet rejected');
      const start = approvals.length * 200;
      for (const d of devices.slice(start, start + tx.getData().commands.length)) enrolled.add(d.objectId);
      approvals.push(tx.getData().commands.length);
      if (uncertain) { uncertain = false; throw Error('confirmation timed out'); }
      return { digest: `digest-${approvals.length}` };
    },
    onProgress: p => progress.push(p),
  };
  await assert.rejects(enrollBudgetDevices(options), /wallet rejected/);
  assert.equal(progress.at(-1).confirmed, 200); assert.equal(progress.at(-1).phase, 'stopped');
  fail = false; uncertain = true;
  await assert.rejects(enrollBudgetDevices(options), /confirmation timed out/);
  assert.equal(progress.at(-1).confirmed, 0); assert.equal(enrolled.size, 400);
  const result = await enrollBudgetDevices(options);
  assert.equal(result.skipped, 400); assert.equal(result.confirmed, 600);
  assert.equal(enrolled.size, 1000); assert.deepEqual(approvals, [200,200,200,200,200]);
  assert.equal(progress.at(-1).phase, 'complete'); assert.equal(result.phase, 'complete');
  await assert.rejects(enrollBudgetDevices({ ...options, devices: [devices[0], devices[0]] }), /Duplicate/);
  let signed = false;
  await assert.rejects(enrollBudgetDevices({ ...options, devices: [devices[0]], isEnrolled: async () => { throw Error('read unavailable'); }, execute: async () => { signed = true; } }), /read unavailable/);
  await assert.rejects(enrollBudgetDevices({ ...options, devices: [devices[0]], isEnrolled: async () => false, readBudget: async () => ({ ...budget, balance: 9n }), execute: async () => { signed = true; } }), /Deposit/);
  assert.equal(signed, false);
  // Mostly-enrolled fleets need funding only for the actual outstanding set.
  let smallCalls = 0;
  const nearlyDone = await enrollBudgetDevices({ ...options,
    isEnrolled: async d => d.objectId !== devices.at(-1).objectId,
    readBudget: async () => ({ ...budget, balance: 10n }),
    execute: async tx => { assert.equal(tx.getData().commands.length, 1); smallCalls++; return { digest: 'last' }; },
  });
  assert.equal(nearlyDone.skipped, 999); assert.equal(smallCalls, 1);
  // A failed execution is never counted as confirmed and stops all later PTBs.
  const failedProgress = [];
  await assert.rejects(enrollBudgetDevices({ ...options, isEnrolled: async () => false,
    execute: async () => { throw Error('failed effects'); }, onProgress: p => failedProgress.push(p),
  }), /failed effects/);
  assert.equal(failedProgress.at(-1).confirmed, 0);
  assert.deepEqual(failedProgress.at(-1).digests, []);
  // A short final chunk remains a separate approval.
  const counts = [];
  await enrollBudgetDevices({ ...options, devices: devices.slice(0, 201), isEnrolled: async () => false,
    execute: async tx => { counts.push(tx.getData().commands.length); return { digest: 'tail' }; },
  });
  assert.deepEqual(counts, [200, 1]);
  // Ineligible identities cannot poison a healthy chunk or its resume.
  const excludedProgress = [];
  const eligibleCounts = [];
  const mixed = { ...options, devices: devices.slice(0, 203), isEnrolled: async () => false,
    ineligibleReason: async d => d === devices[1] ? 'Device identity revoked' : d === devices[2] ? 'Device owner cap changed' : undefined,
    execute: async tx => { eligibleCounts.push(tx.getData().commands.length); return { digest: 'healthy' }; },
    onProgress: p => excludedProgress.push(p),
  };
  const mixedResult = await enrollBudgetDevices(mixed);
  assert.equal(mixedResult.confirmed, 201);
  assert.deepEqual(eligibleCounts, [200, 1]);
  assert.deepEqual(mixedResult.excluded.map(d => d.deviceId), [devices[1].objectId, devices[2].objectId]);
  assert.equal(excludedProgress[0].excluded.length, 0);
  await assert.rejects(enrollBudgetDevices({ ...mixed, ineligibleReason: async () => { throw Error('identity read failed'); } }), /identity read failed/);
  const resumedMixed = await enrollBudgetDevices({ ...mixed, isEnrolled: async d => ![devices[1], devices[2]].includes(d) });
  assert.equal(resumedMixed.confirmed, 0); assert.equal(resumedMixed.skipped, 201); assert.equal(resumedMixed.excluded.length, 2);
  // Preflight refuses oversized / expensive transactions before signing.
  const fake = (size, gas) => ({ setSender() {}, build: async () => new Uint8Array(size), getData: () => ({ gasData: { budget: gas } }) });
  await assert.rejects(prepareEnrollment(fake(120001, '10'), {}, id(1)), /size limit/);
  await assert.rejects(prepareEnrollment(fake(100, '2000000001'), {}, id(1)), /gas ceiling/);
  assert.equal(await prepareEnrollment(fake(100, '123'), {}, id(1)), '123');
  console.log('PASS: 200-command batches, five approvals/1000, partial failure, ambiguous success, chain-read resume, funding/read failures and size/gas guards');
})().catch(e => { console.error(e); process.exitCode = 1; });
