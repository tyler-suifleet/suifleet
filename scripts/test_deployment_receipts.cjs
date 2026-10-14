// Exercise the actual frontend deployment reader against authoritative receipts.
//
//   node scripts/test_deployment_receipts.cjs
//       Unit cases with a scripted client and the fixture manifest.
//   node scripts/test_deployment_receipts.cjs --live MANIFEST.json EXPECTED.json OWNER
//       Real fetchDeploymentEvents with the Sui SDK against the manifest's
//       (loopback) network; EXPECTED maps deployment ID -> {verified, statuses}.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const ts = require("../dapp/node_modules/typescript");
const Module = require("node:module");

const live = process.argv[2] === "--live";
const manifestPath = live ? process.argv[3] : path.resolve(__dirname, "../manifests/fixture.localnet.json");
const fixture = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(fixture);

// Load app TypeScript (and its relative imports) through the real compiler.
const cache = new Map();
function load(filename) {
  if (cache.has(filename)) return cache.get(filename).exports;
  const loaded = new Module(filename, module);
  loaded.filename = filename;
  loaded.paths = Module._nodeModulePaths(path.resolve(__dirname, "../dapp"));
  cache.set(filename, loaded);
  const original = loaded.require.bind(loaded);
  loaded.require = (name) => {
    if (name.startsWith(".")) {
      const candidate = path.resolve(path.dirname(filename), name);
      for (const extension of [".ts", ".tsx"]) if (fs.existsSync(candidate + extension)) return load(candidate + extension);
    }
    return original(name);
  };
  const source = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true, resolveJsonModule: true },
  }).outputText;
  loaded._compile(source, filename);
  return loaded.exports;
}
const sui = load(path.resolve(__dirname, "../dapp/src/lib/sui.ts"));
const receiptsKey = `${fixture.type_origins["deployment_manager::ReceiptsKey"]}::deployment_manager::ReceiptsKey`;

function receipts(entries) {
  return { data: { content: { dataType: "moveObject", fields: { value: { fields: {
    version: "1", statuses: { fields: { contents: entries.map(([key, value]) => ({ fields: { key, value } })) } },
  } } } } } };
}

async function unit() {
  const asked = [];
  const client = {
    getDynamicFieldObject: async ({ parentId, name }) => {
      asked.push(name.type);
      assert.equal(name.type, receiptsKey, "receipts must be read with the manifest's ReceiptsKey origin");
      // A real node rejects {} for an empty struct key ("Missing field dummy_field").
      assert.deepEqual(name.value, { dummy_field: false });
      if (parentId === "0xnew") return receipts([["0xdevice", 1]]);
      if (parentId === "0xlegacy") return { error: { code: "dynamicFieldNotFound", parent_object_id: parentId } };
      if (parentId === "0xbroken") return { data: { content: { dataType: "moveObject", fields: { value: {} } } } };
      return { error: { code: "notExists" } };
    },
  };
  assert.deepEqual(await sui.readReceipts(client, "0xnew"), { verified: true, statuses: { "0xdevice": 1 } });
  // A legacy record whose StatusesKey holds forged "applied" entries: none are read.
  assert.deepEqual(await sui.readReceipts(client, "0xlegacy"), { verified: false, statuses: {} });
  assert.ok(asked.every((type) => !type.endsWith("::StatusesKey")), "legacy StatusesKey must never be read");
  await assert.rejects(sui.readReceipts(client, "0xbroken"), /unexpected Receipts layout/);
  await assert.rejects(sui.readReceipts(client, "0xother"), /notExists/);
  console.log("PASS: receipts reader: verified, legacy-unverified, layout and RPC failures");
}

async function liveRun() {
  const expected = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
  const owner = process.argv[5];
  const { SuiClient } = require("../dapp/node_modules/@mysten/sui/client");
  const deployments = await sui.fetchDeploymentEvents(new SuiClient({ url: fixture.rpc_url }), owner);
  const seen = Object.fromEntries(deployments.map((d) => [d.deploymentId, { verified: d.receiptsVerified, statuses: d.statuses }]));
  for (const [id, want] of Object.entries(expected)) {
    assert.deepEqual(seen[id], want, `deployment ${id}`);
  }
  console.log(`PASS: fetchDeploymentEvents on live records: ${JSON.stringify(seen)}`);
}

(live ? liveRun() : unit()).catch((error) => { console.error(error); process.exit(1); });
