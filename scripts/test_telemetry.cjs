// Device telemetry (SF-019) in the dApp: independent signature verification,
// shared status cases, chain identity reads and the fleet health query.
//   node scripts/test_telemetry.cjs
//   node scripts/test_telemetry.cjs --verify ENVELOPES.json SIGNER DEVICE CHAIN REGISTRY
// The second form verifies envelopes captured from the real daemon
// (scripts/test_telemetry.py) with the dApp's own verifier.
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const { loader, appRequire } = require("./frontend_test_utils.cjs");
const cases = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../interfaces/telemetry-cases.json")));
const fixture = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../manifests/fixture.localnet.json")));
const verifyMode = process.argv[2] === "--verify";
const [chain, registry] = verifyMode ? process.argv.slice(6, 8) : [cases.signed.snapshot.chain_identifier, cases.signed.snapshot.registry];
const m = { ...fixture, chain_identifier: chain, shared_objects: { registry: { ...fixture.shared_objects.registry, id: registry } } };
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
const telemetry = loader()("lib/telemetry.ts");
const protocol = loader()("lib/fleet-protocol.ts");
const { normalizeSuiAddress: id, toBase64, fromBase64 } = appRequire("@mysten/sui/utils");
const { ObjectError } = appRequire("@mysten/sui/client");
const { Ed25519Keypair } = appRequire("@mysten/sui/keypairs/ed25519");
const { Secp256r1Keypair } = appRequire("@mysten/sui/keypairs/secp256r1");

async function verifyCaptured([file, signer, device]) {
  const envelopes = JSON.parse(fs.readFileSync(file));
  assert.ok(envelopes.length > 0, "no envelopes captured");
  for (const envelope of envelopes) {
    const snapshot = await telemetry.verifyReport(envelope, device, signer);
    assert.equal(typeof snapshot, "object", `daemon envelope rejected by the dApp: ${snapshot}`);
  }
  // A changed byte or another identity never verifies.
  const bytes = fromBase64(envelopes[0].message);
  bytes[bytes.length - 2] ^= 1;
  assert.equal(await telemetry.verifyReport({ ...envelopes[0], message: toBase64(bytes) }, device, signer), "signature is not the device identity's");
  assert.equal(await telemetry.verifyReport(envelopes[0], device, id("0x99")), "signature is not the device identity's");
  console.log(`PASS: the dApp verifies all ${envelopes.length} daemon telemetry envelopes against the device identity`);
}

function identityField(signer, revoked, type = m.type_origins["device_registry::Identity"] + "::device_registry::Identity") {
  return { dynamicField: { value: { type, bcs: protocol.device_registry_IdentityBcs.serialize({ signer, revoked, owner_cap: id("0xc0") }).toBytes() } } };
}

async function sign(keypair, snapshot) {
  const bytes = new TextEncoder().encode(JSON.stringify(snapshot));
  return { message: toBase64(bytes), signature: (await keypair.signPersonalMessage(bytes)).signature };
}

(async () => {
  if (verifyMode) return verifyCaptured(process.argv.slice(3));
  const { signer, snapshot, envelope } = cases.signed;
  const device = snapshot.device_id;

  // The shared vector (also produced by the daemon and verified by the indexer).
  assert.deepEqual(await telemetry.verifyReport(envelope, device, signer), snapshot);
  const tampered = fromBase64(envelope.message);
  tampered[tampered.length - 2] ^= 1;
  assert.equal(await telemetry.verifyReport({ ...envelope, message: toBase64(tampered) }, device, signer), "signature is not the device identity's");
  assert.equal(await telemetry.verifyReport(envelope, device, id("0x99")), "signature is not the device identity's");
  assert.equal(await telemetry.verifyReport(envelope, "0xd2", signer), "report is for another device");
  assert.equal(await telemetry.verifyReport({ message: "!!", signature: "!!" }, device, signer), "undecodable report");
  const multisig = fromBase64(envelope.signature);
  multisig[0] = 0x03;
  assert.equal(await telemetry.verifyReport({ ...envelope, signature: toBase64(multisig) }, device, signer), "unsupported signature scheme");
  console.log("PASS: shared telemetry vector verifies; tampering, other identities and devices, other schemes are refused");

  // Binding checks run after the signature: a valid signature is not enough.
  const ed = Ed25519Keypair.fromSecretKey(new Uint8Array(32).fill(7));
  for (const [change, expected] of [
    [{ chain_identifier: "4c78adac" }, "report is bound to another deployment"],
    [{ registry: "0x6" }, "report is bound to another deployment"],
    [{ schema: 2 }, "unsupported telemetry schema 2"],
    [{ signer: id("0x99") }, "report names another signer"],
  ]) {
    assert.equal(await telemetry.verifyReport(await sign(ed, { ...snapshot, ...change }), device, signer), expected);
  }
  // PKCS#11 devices sign with P-256 (SF-021).
  const p256 = Secp256r1Keypair.fromSecretKey(new Uint8Array(32).fill(9));
  const p256Snapshot = { ...snapshot, signer: p256.toSuiAddress() };
  assert.deepEqual(await telemetry.verifyReport(await sign(p256, p256Snapshot), device, p256.toSuiAddress()), p256Snapshot);
  console.log("PASS: chain/registry/schema/signer binding enforced; P-256 device signatures verify");

  // Status rules shared with the indexer.
  for (const c of cases.status) {
    const latest = c.snapshot ? { snapshot: c.snapshot, receivedMs: c.received_ms } : undefined;
    const result = telemetry.assess(!!c.revoked, latest, c.now_ms);
    assert.equal(result.status, c.status, c.name);
    assert.deepEqual(result.codes, c.codes, c.name);
    assert.equal(result.reasons.length > 0, c.codes.length > 0, c.name);
  }
  console.log(`PASS: ${cases.status.length} shared status cases match the indexer's rules`);

  // Identity reads: absent means not activated; wrong types and transport errors surface.
  const keys = [];
  const read = await telemetry.readIdentity({ getDynamicField: async (args) => { keys.push(args); return identityField(signer, false); } }, device);
  assert.deepEqual(read, { signer, revoked: false });
  assert.equal(keys[0].parentId, registry);
  assert.equal(keys[0].name.type, `${m.type_origins["device_registry::IdentityKey"]}::device_registry::IdentityKey`);
  assert.equal(Buffer.from(keys[0].name.bcs).toString("hex"), id(device).slice(2));
  const missing = { getDynamicField: async () => { throw new ObjectError("missing", "not found", { reason: "notFound" }); } };
  assert.equal(await telemetry.readIdentity(missing, device), undefined);
  await assert.rejects(telemetry.readIdentity({ getDynamicField: async () => identityField(signer, false, "0x2::coin::Coin") }, device), /Unexpected Identity type/);
  await assert.rejects(telemetry.readIdentity({ getDynamicField: async () => { throw new ObjectError("bad", "transport unavailable", { reason: "unknown" }); } }, device), /transport unavailable/);
  console.log("PASS: identity read from the registry dynamic field; missing/foreign/transport cases distinct");

  // Fleet query: verified against the chain identity, never the indexer's word.
  const healthyNow = { ...snapshot, health: "healthy", reasons: [], components: [snapshot.components[0]] };
  const fresh = await sign(ed, healthyNow);
  const report = (s, received) => ({ device_id: id(device), seq: s.seq, signer, observed_ms: s.observed_ms, received_ms: received, ...s.envelope });
  const now = healthyNow.observed_ms + 1000;
  const identities = { [id(device)]: identityField(signer, false), [id("0xd2")]: identityField(signer, false),
                       [id("0xd3")]: identityField(signer, true), [id("0xd5")]: identityField(signer, false) };
  const client = { getDynamicField: async (args) => {
    const key = id(Buffer.from(args.name.bcs).toString("hex"));
    if (!identities[key]) throw new ObjectError("missing", "not found", { reason: "notFound" });
    return identities[key];
  } };
  const forged = await sign(Ed25519Keypair.fromSecretKey(new Uint8Array(32).fill(8)), { ...healthyNow, device_id: "0xd2" });
  const pages = {
    "": { items: [
      { device_id: id(device), report: report({ ...healthyNow, envelope: fresh }, healthyNow.observed_ms) },
      { device_id: id("0xd2"), report: report({ ...healthyNow, envelope: forged }, now) },
    ], next: "p2", server_time_ms: now },
    p2: { items: [{ device_id: id("0xd3"), report: report({ ...healthyNow, envelope: fresh }, now) },
                  { device_id: id("0xd5"), report: null }], next: null, server_time_ms: now },
  };
  const requests = [];
  const fetcher = async (url, init) => {
    requests.push({ url, init });
    const cursor = new URL(url).searchParams.get("cursor") ?? "";
    return { ok: true, status: 200, json: async () => pages[cursor] };
  };
  const owner = id("0x0a");
  const health = await telemetry.fetchFleetHealth(client, [device, "0xd2", "0xd3", "0xd4", "0xd5"], owner, "https://fleet.example/", "tok", fetcher);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].url, `https://fleet.example/v1/health?owner=${owner}&limit=500`);
  assert.equal(requests[0].init.headers.authorization, "Bearer tok");
  assert.equal(health[device].status, "healthy");
  assert.equal(health[device].lastSeenAgeMs, 1000);
  assert.equal(health[device].components[0].name, "firmware");
  assert.equal(telemetry.lastSeenLabel(health[device]), "1 s ago");
  assert.deepEqual([health["0xd2"].status, health["0xd2"].codes], ["unverified", ["unverified"]]);
  assert.deepEqual([health["0xd3"].status, health["0xd3"].codes], ["unreported", ["revoked"]]);
  assert.deepEqual([health["0xd4"].status, health["0xd4"].codes], ["unreported", ["not-activated"]]);
  assert.deepEqual([health["0xd5"].status, health["0xd5"].codes], ["unreported", ["no-telemetry"]]);
  assert.equal(telemetry.lastSeenLabel(health["0xd5"]), "never");
  console.log("PASS: fleet health paginates, verifies each report against the chain identity, flags forged/revoked/unactivated/unreported");

  // Unavailable service or configuration: nothing is claimed.
  for (const [url, fetch] of [["", fetcher], ["https://fleet.example", async () => ({ ok: false, status: 401 })],
                              ["https://fleet.example", async () => { throw new Error("connection refused"); }]]) {
    const result = await telemetry.fetchFleetHealth(client, [device], owner, url, "", fetch);
    assert.equal(result[device].status, "unavailable");
  }
  const stuck = async () => ({ ok: true, status: 200, json: async () => ({ items: [], next: "same", server_time_ms: now }) });
  await assert.rejects(telemetry.fetchIndexedHealth("https://fleet.example", "", owner, stuck), /made no progress/);
  console.log("PASS: missing configuration, refused token and unreachable service show health unavailable; stuck pagination fails");

  // A failed refresh never leaves earlier health on screen: the query holds the
  // last successful data on error, so the display (useFleetHealth) expires it.
  const { QueryClient } = appRequire("@tanstack/react-query");
  const queries = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const key = ["health", device];
  const onePage = async () => ({ ok: true, status: 200, json: async () => ({ items: [pages[""].items[0]], next: null, server_time_ms: now }) });
  const shown = (atMs) => {
    const state = queries.getQueryState(key);
    return telemetry.currentHealth(state.data, state.dataUpdatedAt, state.status === "error", atMs)[device];
  };
  await queries.fetchQuery({ queryKey: key, queryFn: () => telemetry.fetchFleetHealth(client, [device], owner, "https://fleet.example", "", onePage) });
  const verifiedAt = queries.getQueryState(key).dataUpdatedAt;
  assert.equal(shown(verifiedAt).status, "healthy");
  assert.equal(shown(verifiedAt + telemetry.HEALTH_STALE_MS).status, "healthy");
  // Chain identity outage on the next refresh: that device is unavailable, the refresh still succeeds.
  const down = { getDynamicField: async () => { throw new ObjectError("bad", "chain identity RPC unavailable", { reason: "unknown" }); } };
  await queries.fetchQuery({ queryKey: key, staleTime: 0, queryFn: () => telemetry.fetchFleetHealth(down, [device], owner, "https://fleet.example", "", onePage) });
  assert.equal(queries.getQueryState(key).status, "success");
  assert.deepEqual([shown(Date.now()).status, shown(Date.now()).reasons], ["unavailable", ["device identity cannot be read from the chain: chain identity RPC unavailable"]]);
  // A report whose check throws is unavailable too, not a rejected refresh.
  const throwing = async () => ({ ok: true, status: 200, json: async () => ({ items: [{ device_id: id(device), report: { ...pages[""].items[0].report, get received_ms() { throw new Error("malformed report"); } } }], next: null, server_time_ms: now }) });
  const unchecked = await telemetry.fetchFleetHealth(client, [device], owner, "https://fleet.example", "", throwing);
  assert.deepEqual([unchecked[device].status, unchecked[device].reasons], ["unavailable", ["newest report cannot be checked: malformed report"]]);
  // Back to healthy, then a refresh that rejects outright: the retained data is not shown.
  await queries.fetchQuery({ queryKey: key, staleTime: 0, queryFn: () => telemetry.fetchFleetHealth(client, [device], owner, "https://fleet.example", "", onePage) });
  assert.equal(shown(Date.now()).status, "healthy");
  await assert.rejects(queries.fetchQuery({ queryKey: key, staleTime: 0, queryFn: async () => { throw new Error("unexpected failure"); } }));
  const failed = queries.getQueryState(key);
  assert.equal(failed.status, "error");
  assert.equal(failed.data[device].status, "healthy", "TanStack Query keeps the earlier data on error");
  assert.deepEqual([shown(Date.now()).status, shown(Date.now()).reasons], ["unavailable", ["health refresh failed"]]);
  // A refresh that never finishes: health expires, and stays unavailable well past the offline threshold.
  await queries.fetchQuery({ queryKey: key, staleTime: 0, queryFn: () => telemetry.fetchFleetHealth(client, [device], owner, "https://fleet.example", "", onePage) });
  const lastOk = queries.getQueryState(key).dataUpdatedAt;
  const offlineMs = telemetry.OFFLINE_INTERVALS * healthyNow.interval_seconds * 1000;
  for (const later of [telemetry.HEALTH_STALE_MS + 1, offlineMs + 1, 10 * offlineMs]) {
    assert.equal(shown(lastOk + later).status, "unavailable", `${later} ms after the last refresh`);
  }
  assert.equal(telemetry.currentHealth(undefined, 0, true, now), undefined);
  queries.clear();
  console.log("PASS: identity outage or failed/stalled refresh shows health unavailable, never the earlier healthy result");
})().catch((error) => { console.error(error); process.exit(1); });
