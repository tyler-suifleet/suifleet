const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { loader, appRequire } = require("./frontend_test_utils.cjs");
const live = process.argv[2] === "--live";
const fixture = JSON.parse(fs.readFileSync(live ? process.argv[3] : path.resolve(__dirname, "../manifests/fixture.localnet.json"), "utf8"));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(fixture);
const load = loader();
const sui = load("lib/sui.ts"), { ReceiptsBcs } = load("lib/move-bcs.ts");
const { ObjectError } = appRequire("@mysten/sui/client");
const { normalizeSuiAddress } = appRequire("@mysten/sui/utils");
(async () => {
  if (live) {
    const { SuiGrpcClient } = appRequire("@mysten/sui/grpc");
    const deployments = await sui.fetchDeploymentEvents(new SuiGrpcClient({ network: fixture.network, baseUrl: fixture.rpc_url }), process.argv[5]);
    const seen = Object.fromEntries(deployments.map((d) => [d.deploymentId, { verified: d.receiptsVerified, statuses: d.statuses }]));
    for (const [id, expected] of Object.entries(JSON.parse(fs.readFileSync(process.argv[4], "utf8")))) assert.deepEqual(seen[id], expected);
    console.log("PASS: gRPC live authoritative deployment receipts"); return;
  }
  const asked = [];
  const client = { getDynamicField: async ({ parentId, name }) => {
    asked.push(name.type);
    assert.equal(name.type, `${fixture.type_origins["deployment_manager::ReceiptsKey"]}::deployment_manager::ReceiptsKey`);
    assert.deepEqual([...name.bcs], [0]); // implicit dummy_field=false
    if (parentId === "0xlegacy") throw new ObjectError("notExists", "missing field", { reason: "notFound" });
    if (parentId === "0xother") throw new Error("transport unavailable");
    return { dynamicField: { value: { type: `${fixture.type_origins["deployment_manager::Receipts"]}::deployment_manager::Receipts`, bcs:
      parentId === "0xbroken" ? new Uint8Array() : ReceiptsBcs.serialize({ version: parentId === "0xfuture" ? 2 : parentId === "0xinvalid" ? 0 : 1,
        statuses: { contents: [{ key: "0xde", value: parentId === "0xbadstatus" ? 9 : 1 }] } }).toBytes() } } };
  } };
  assert.deepEqual(await sui.readReceipts(client, "0xnew"), { verified: true, statuses: { [normalizeSuiAddress("0xde")]: 1 } });
  assert.deepEqual(await sui.readReceipts(client, "0xlegacy"), { verified: false, statuses: {} });
  assert.ok(asked.every((type) => !type.endsWith("::StatusesKey")));
  await assert.rejects(sui.readReceipts(client, "0xbroken"));
  await assert.rejects(sui.readReceipts(client, "0xother"), /transport unavailable/);
  assert.equal((await sui.readReceipts(client, "0xfuture")).verified, true);
  await assert.rejects(sui.readReceipts(client, "0xinvalid"), /Invalid Receipts/);
  await assert.rejects(sui.readReceipts(client, "0xbadstatus"), /Unsupported Receipts/);
  console.log("PASS: BCS receipts, legacy missing field, layout/version/status and transport errors");
})().catch((error) => { console.error(error); process.exitCode = 1; });
