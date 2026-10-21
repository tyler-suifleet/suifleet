// SF-100 enrollment library, offline: rosters, naming templates, conflicts,
// claim credentials and archives, job files, chain-derived statuses (binding
// only through the claim's enrollment and the operator's owner cap) and
// transaction-sized plans. Real application modules and SDK.
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const { loader, appRequire, manifestClient } = require("./frontend_test_utils.cjs");
const m = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../manifests/fixture.localnet.json")));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
const load = loader(), e = load("lib/enrollment.ts"), protocol = load("lib/fleet-protocol.ts");
const { normalizeSuiAddress: id, fromHex } = appRequire("@mysten/sui/utils");
const { Ed25519Keypair } = appRequire("@mysten/sui/keypairs/ed25519");
const { ObjectError } = appRequire("@mysten/sui/client");
const operator = id("0xa"), stranger = id("0xb"), policyId = id("0x9001"), registry = id(m.shared_objects.registry.id);
const storage = () => { const map = new Map(); return { get length() { return map.size; }, key: (i) => [...map.keys()][i] ?? null,
  getItem: (k) => map.get(k) ?? null, setItem: (k, v) => map.set(k, String(v)), removeItem: (k) => map.delete(k) }; };
const device = (n, extra = {}) => ({ objectId: id(`0xd${n}`), name: `dev-${n}`, chainName: `dev-${n}`, meta: {}, deviceAddress: id(`0xe${n}`),
  arch: "aarch64", registeredAt: 1, ownerCapId: id(`0xc${n}`), ...extra });
const group = (n, name, deviceIds = []) => ({ objectId: id(`0x6${n}`), name, description: "", admin: operator, deviceIds });
const commands = (tx) => tx.getData().commands;

(async () => {
  // ── CSV and naming templates ─────────────────────────────────────────────
  assert.deepEqual(e.parseCsv('serial,alias\r\n"SN,1","a ""b"""\n\n2,\n'), [["serial", "alias"], ["SN,1", 'a "b"'], ["2", ""]]);
  assert.throws(() => e.parseCsv('serial\n"open'), /quoted/);
  assert.equal(e.expandTemplate("pi-{n:4}-{serial}", 7, "X1"), "pi-0007-X1");
  let roster = e.parseRoster("Serial,Name,Group\nSN1,,lab\nSN2,kitchen,\nSN3,,\n", "pi-{n:3}", 41);
  assert.deepEqual(roster.rows.map((r) => [r.line, r.serial, r.alias, r.group]), [[2, "SN1", "pi-041", "lab"], [3, "SN2", "kitchen", undefined], [4, "SN3", "pi-043", undefined]]);
  assert.deepEqual(e.parseRoster("serial\nSN1\n").issues.map((i) => i.message), ["no alias and no naming template"]);
  assert.match(e.parseRoster("alias\nx\n").issues[0].message, /serial column/);

  // ── Reservation checks ───────────────────────────────────────────────────
  const devices = [device(1, { name: "taken", serial: "SN-TAKEN" })];
  const groups = [group(1, "lab"), group(2, "dup"), group(3, "dup")];
  const rows = [
    { line: 2, serial: "SN-1", alias: "ok-1", group: "lab" },
    { line: 3, serial: "SN-2", alias: "ok-2", group: groups[2].objectId },
    { line: 4, serial: "SN-3", alias: "ok-3" },
    { line: 5, serial: "sn-1", alias: "other" },                       // duplicate serial (case-insensitive)
    { line: 6, serial: "SN-4", alias: "OK-1" },                        // duplicate alias
    { line: 7, serial: "SN-5", alias: "TAKEN" },                       // an existing device's name
    { line: 8, serial: "sn-taken", alias: "x" },                       // an existing device's serial
    { line: 9, serial: "SN-6", alias: "held" },                        // reserved in another job
    { line: 10, serial: "SN-7", alias: 'x"\nkeypair_path = "/tmp/k' }, // TOML injection
    { line: 11, serial: "../etc", alias: "y" },                        // path traversal
    { line: 12, serial: "SN-8", alias: "z", group: "dup" },            // ambiguous group name
    { line: 13, serial: "SN-9", alias: "w", group: "nope" },           // not the operator's group
  ];
  const check = e.checkReservations(rows, { devices, groups, reserved: [{ serial: "R1", alias: "held" }], defaultGroupId: groups[0].objectId });
  assert.deepEqual(check.accepted, [
    { serial: "SN-1", alias: "ok-1", groupId: groups[0].objectId },
    { serial: "SN-2", alias: "ok-2", groupId: groups[2].objectId },
    { serial: "SN-3", alias: "ok-3", groupId: groups[0].objectId },
  ]);
  assert.deepEqual(check.issues.map((i) => i.line), [5, 6, 7, 8, 9, 10, 11, 12, 13]);
  assert.match(check.issues.find((i) => i.line === 7).message, /already used by device taken/);
  assert.match(check.issues.find((i) => i.line === 12).message, /ambiguous/);
  assert.match(check.issues.find((i) => i.line === 13).message, /not one of your groups/);

  // ── Credentials and the archive ──────────────────────────────────────────
  const { entries, credentials } = e.generateCredentials(check.accepted);
  assert.equal(new Set(entries.map((x) => x.claim)).size, 3);
  for (const [i, c] of credentials.entries()) {
    assert.match(c.seedHex, /^[0-9a-f]{64}$/);
    assert.equal(Ed25519Keypair.fromSecretKey(fromHex(c.seedHex)).toSuiAddress(), c.claim);
    assert.equal(entries[i].claim, c.claim);
    assert.ok(!JSON.stringify(entries[i]).includes(c.seedHex), "a job entry carries key material");
  }
  assert.throws(() => e.generateCredentials(Array.from({ length: e.BROWSER_CREDENTIAL_LIMIT + 1 }, (_, i) => ({ serial: `S${i}`, alias: `A${i}` }))), /claims\.py/);
  const archive = e.credentialArchive(credentials, policyId, JSON.stringify(m));
  const files = new Map();
  for (let offset = 0; offset + 512 <= archive.length;) {
    const header = archive.subarray(offset, offset + 512);
    if (header.every((b) => b === 0)) break;
    const text = (a, b) => Buffer.from(header.subarray(a, b)).toString("latin1").replace(/\0.*$/s, "");
    const sum = header.reduce((s, b, i) => s + (i >= 148 && i < 156 ? 32 : b), 0);
    assert.equal(parseInt(text(148, 156), 8), sum, "bad tar checksum");
    assert.equal(text(257, 263), "ustar");
    const size = parseInt(text(124, 136), 8);
    files.set(text(0, 100), { mode: parseInt(text(100, 108), 8), type: text(156, 157), body: Buffer.from(archive.subarray(offset + 512, offset + 512 + size)).toString() });
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  assert.equal(files.get("enrollment/").type, "5");
  const c0 = credentials[0], toml = files.get(`enrollment/${c0.serial}/enroll.toml`);
  assert.equal(files.get(`enrollment/${c0.serial}/claim.key`).body, `${c0.seedHex}\n`);
  assert.equal(files.get(`enrollment/${c0.serial}/claim.key`).mode, 0o600);
  assert.equal(files.get(`enrollment/${c0.serial}/`).mode, 0o700);
  assert.deepEqual(JSON.parse(files.get(`enrollment/${c0.serial}/deployment.json`).body), m);
  for (const line of [`policy_id = "${policyId}"`, `name = "${c0.alias}"`, 'claim_key_path = "claim.key"', 'manifest_path = "deployment.json"', 'keypair_path = "/etc/suifleet/device.key"']) {
    assert.ok(toml.body.split("\n").includes(line), `enroll.toml lacks ${line}`);
  }
  assert.equal(files.size, 1 + credentials.length * 4);
  assert.ok(!toml.body.includes(c0.seedHex));

  // ── Job files: public data only, bound to operator/policy/fleet/network ──
  const job = e.newJob(operator, policyId);
  job.entries.push(...entries);
  const exported = JSON.stringify(job);
  assert.deepEqual(e.parseJob(exported, { operator, policyId }).entries, entries);
  for (const [text, pattern] of [
    [JSON.stringify({ ...job, entries: [{ ...entries[0], seed: credentials[0].seedHex }] }), /key material/],
    [`${exported.slice(0, -1)},"x":"suiprivkey1abc"}`, /key material/],
    [JSON.stringify({ ...job, operator: stranger }), /another operator/],
    [JSON.stringify({ ...job, policyId: id("0x9002") }), /another enrollment policy/],
    [JSON.stringify({ ...job, registry: id("0x77") }), /another fleet registry/],
    [JSON.stringify({ ...job, network: "testnet" }), /not localnet/],
    [JSON.stringify({ ...job, entries: [entries[0], entries[0]] }), /Duplicate claim/],
    [JSON.stringify({ ...job, entries: [{ ...entries[0], alias: "a b" }] }), /Invalid entry/],
  ]) assert.throws(() => e.parseJob(text, { operator, policyId }), pattern);
  // A claims.py roster: unresolved group names survive until import resolves them.
  const cli = e.parseJob(JSON.stringify({ schema: 1, claimFundMist: "30000000", entries: [{ serial: "C1", alias: "cli-1", group: "lab", claim: id("0x51") }] }), { operator, policyId });
  assert.equal(cli.entries[0].group, "lab");
  assert.equal(cli.claimFundMist, "30000000");
  const store = storage();
  e.saveJob(store, job);
  assert.deepEqual(e.loadJob(store, operator, policyId).entries, entries);
  assert.deepEqual(e.storedReservations(store).map((r) => r.serial), ["SN-1", "SN-2", "SN-3"]);
  assert.ok(![...Array(store.length).keys()].some((i) => store.getItem(store.key(i)).includes(c0.seedHex)), "a key reached storage");

  // ── Statuses: only chain state, only through the claim's enrollment ──────
  const fund = BigInt(job.claimFundMist);
  const claim = (n) => id(`0x${(0xc1a0 + n).toString(16)}`);
  const entry = (n, extra = {}) => ({ serial: `S${n}`, alias: `a${n}`, claim: claim(n), ...extra });
  const bound = (n, extra = {}) => device(n, { meta: { "suifleet.alias": `a${n}`, "suifleet.serial": `S${n}` }, name: `a${n}`, serial: `S${n}`, ...extra });
  const enrolled = (n, extra = {}) => [claim(n), { policyId, operator, claim: claim(n), deviceId: id(`0xd${n}`), deviceAddress: id(`0xe${n}`), ownerCap: id(`0xc${n}`), ...extra }];
  const sJob = { ...e.newJob(operator, policyId), entries: [
    entry(1), entry(2), entry(3), entry(4, { revoked: true }), entry(5, { submitted: true }),
    entry(6), entry(7), entry(8), entry(9), entry(10, { groupId: id("0x6ff") }), entry(11, { groupId: groups[0].objectId }), entry(12, { groupId: groups[0].objectId }),
  ] };
  const snap = {
    policy: {}, registered: new Set([claim(2), claim(3)]), balances: new Map([[claim(2), fund - BigInt(1)], [claim(3), fund]]),
    enrollments: new Map([enrolled(6, { operator: stranger }), enrolled(7), enrolled(8, { ownerCap: id("0xcc") }), enrolled(9), enrolled(10), enrolled(11), enrolled(12)]),
    devices: [device(8), device(9), device(99, { name: "A9" }), bound(10), device(11), bound(12)],
    groups: [group(1, "lab", [id("0xd12")])],
  };
  const views = e.entryStatuses(sJob, snap);
  assert.deepEqual(views.map((v) => v.status), ["unregistered", "unfunded", "awaiting-device", "revoked", "withdrawn",
    "untrusted", "untrusted", "untrusted", "conflict", "conflict", "binding", "complete"]);
  assert.match(views[5].detail, /another operator/);
  assert.match(views[6].detail, /owner cap/);
  assert.match(views[8].detail, /already used by A9/);
  assert.match(views[9].detail, /group/);
  assert.deepEqual([views[10].needsMeta, views[10].needsGroup], [true, true]);

  // ── Plans: chunked, idempotent top-ups, bounded commands ─────────────────
  const many = { ...e.newJob(operator, policyId), entries: Array.from({ length: 600 }, (_, i) => entry(100 + i)) };
  const top = claim(100);
  const pSnap = { ...snap, registered: new Set([top]), balances: new Map([[top, BigInt(5_000_000)]]), enrollments: new Map(), devices: [], groups: [] };
  const steps = e.plan(many, e.entryStatuses(many, pSnap), pSnap);
  assert.deepEqual(steps.map((s) => [s.kind, s.entries.length]), [["claims", 250], ["claims", 250], ["claims", 100]]);
  const first = commands(steps[0].build());
  assert.equal(first.length, 252);
  assert.ok(first.length <= 1024);
  const added = first[0].MoveCall;
  assert.equal(`${added.module}::${added.function}`, "enrollment::add_claims");
  const firstTx = steps[0].build().getData();
  const pureArg = firstTx.inputs[added.arguments[1].Input];
  // The already-registered claim is not re-added, only topped up to the job's amount.
  assert.equal(Buffer.from(pureArg.Pure.bytes, "base64").length, 2 + 249 * 32); // ULEB128 length, then addresses
  const amounts = first[1].SplitCoins.amounts.map((a) => BigInt(appRequire("@mysten/sui/bcs").bcs.u64().parse(Buffer.from(firstTx.inputs[a.Input].Pure.bytes, "base64"))));
  assert.equal(amounts[0], fund - BigInt(5_000_000));
  assert.ok(amounts.slice(1).every((a) => a === fund));
  assert.ok(Buffer.from(pureArg.Pure.bytes, "base64").length <= 16 * 1024);

  const binds = { ...e.newJob(operator, policyId), entries: Array.from({ length: 230 }, (_, i) => entry(1000 + i, { groupId: groups[0].objectId })) };
  const bSnap = { ...pSnap, registered: new Set(), enrollments: new Map(binds.entries.map((_, i) => enrolled(1000 + i))),
    devices: binds.entries.map((_, i) => device(1000 + i)), groups: [group(1, "lab")] };
  const bindSteps = e.plan(binds, e.entryStatuses(binds, bSnap), bSnap);
  assert.deepEqual(bindSteps.map((s) => [s.kind, s.entries.length]), [["bind", 100], ["bind", 100], ["bind", 30]]);
  const bindCommands = commands(bindSteps[0].build());
  assert.equal(bindCommands.length, 500);
  assert.deepEqual(bindCommands.slice(0, 5).map((c) => c.MoveCall.function), ["borrow_device", "set_meta", "set_meta", "add_device", "return_device"]);
  assert.throws(() => e.buildBindTx([views[11]]), /not ready/);
  assert.throws(() => e.buildBindTx(Array(101).fill(views[10])), /Too many/);
  assert.throws(() => e.buildRevokeClaimsTx(policyId, Array(251).fill(top)), /Too many/);
  assert.equal(commands(e.buildRevokeClaimsTx(policyId, [top, claim(101)])).length, 2);
  assert.equal(commands(e.buildTransferTx([id("0xe1"), id("0xe2")], BigInt(7))).length, 3);
  assert.deepEqual(e.chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]]);

  // ── Chain reads: policy identity, cross-fleet refusal, claim lookups ─────
  const policyType = `${m.type_origins["enrollment::EnrollmentPolicy"]}::enrollment::EnrollmentPolicy`;
  const policyObject = (owner = operator, reg = registry, type = policyType) => ({ object: { objectId: policyId, type,
    content: protocol.enrollment_EnrollmentPolicyBcs.serialize({ id: policyId, operator: owner, registry: reg, claims: { id: id("0x7ab"), size: 2 },
      remaining: 5, expires_at_ms: 0, open: true, enrolled: 1 }).toBytes() } });
  function chainClient(policy) {
    const c = manifestClient(m), base = c.getObject;
    c.getObject = async (args) => args.objectId === policyId ? policy : base(args);
    c.listOwnedObjects = async () => ({ objects: [], hasNextPage: false });
    c.listEvents = async ({ filter }) => ({ hasNextPage: false, events: !filter.eventType.endsWith("::DeviceEnrolled") ? [] : [
      { bcs: protocol.enrollment_DeviceEnrolledBcs.serialize({ policy_id: policyId, device_id: id("0xd7"), device_address: id("0xe7"), claim: claim(7), operator, owner_cap: id("0xc7"), device_cap: id("0xf7") }).toBytes() },
      { bcs: protocol.enrollment_DeviceEnrolledBcs.serialize({ policy_id: id("0x9002"), device_id: id("0xd8"), device_address: id("0xe8"), claim: claim(8), operator, owner_cap: id("0xc8"), device_cap: id("0xf8") }).toBytes() },
    ] });
    c.getDynamicField = async ({ parentId, name }) => {
      assert.equal(parentId, id("0x7ab"));
      const key = id(Buffer.from(name.bcs).toString("hex"));
      if (key === claim(3)) throw new Error("transport down");
      if (key !== claim(2)) throw new ObjectError("missing", "not found", { reason: "notFound" });
      return { dynamicField: { value: { type: "bool", bcs: new Uint8Array([1]) } } };
    };
    c.getBalance = async ({ owner }) => ({ balance: { balance: owner === claim(2) ? "123" : "0" } });
    return c;
  }
  const okJob = { ...e.newJob(operator, policyId), entries: [entry(2), entry(7), entry(8)] };
  const state = await e.snapshot(chainClient(policyObject()), okJob);
  assert.deepEqual([...state.enrollments.keys()], [claim(7)], "another policy's enrollment was attributed");
  assert.deepEqual([...state.registered], [claim(2)]);
  assert.equal(state.balances.get(claim(2)), BigInt(123));
  await assert.rejects(e.snapshot(chainClient(policyObject(stranger)), okJob), /another operator/);
  await assert.rejects(e.snapshot(chainClient(policyObject(operator, id("0x77"))), okJob), /another fleet registry/);
  await assert.rejects(e.snapshot(chainClient(policyObject(operator, registry, `${id("0x99")}::enrollment::EnrollmentPolicy`)), okJob), /Not an EnrollmentPolicy/);
  await assert.rejects(e.snapshot(chainClient(policyObject()), { ...okJob, entries: [entry(3)] }), /transport down/);
  assert.equal(e.policyAccepting({ open: true, expiresAtMs: BigInt(10), remaining: BigInt(1) }, 11), "expired");
  assert.equal(e.policyAccepting({ open: false, expiresAtMs: BigInt(0), remaining: BigInt(1) }), "closed");
  assert.equal(e.policyAccepting({ open: true, expiresAtMs: BigInt(0), remaining: BigInt(0) }), "quota exhausted");
  assert.equal(e.policyAccepting({ open: true, expiresAtMs: BigInt(0), remaining: BigInt(1) }), undefined);
  console.log("PASS: enrollment rosters/templates/conflicts, credential archive (0600 keys, enroll.toml), key-free jobs bound to operator/policy/fleet/network, "
    + "chain-derived statuses (foreign operator, missing owner cap, conflicts, withdrawn), chunked plans within PTB limits and cross-fleet policy refusal");
})().catch((error) => { console.error(error); process.exitCode = 1; });
