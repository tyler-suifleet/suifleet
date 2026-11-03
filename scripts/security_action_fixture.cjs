// Canonical stable object identities from the production SDK; no signing keys.
const { appRequire } = require("./frontend_test_utils.cjs");
const assert = require("node:assert/strict");
const { SuiGrpcClient } = appRequire("@mysten/sui/grpc");
const { bcs } = appRequire("@mysten/sui/bcs");
(async () => {
  const [rpc, customer, fleet, origin, name] = process.argv.slice(2);
  const client = new SuiGrpcClient({ network: "testnet", baseUrl: rpc });
  const c = (await client.getObject({ objectId: customer, include: { content: true } })).object;
  const f = (await client.getObject({ objectId: fleet, include: { content: true } })).object;
  assert.equal(c.type, origin + "::customer::Customer");
  assert.equal(f.type, origin + "::customer::Fleet");
  const values = [bcs.string().serialize("rename_fleet").toBytes(), bcs.Address.serialize(customer).toBytes(), bcs.Address.serialize(fleet).toBytes(), bcs.string().serialize(name).toBytes()];
  process.stdout.write(JSON.stringify([...bcs.vector(bcs.vector(bcs.u8())).serialize(values).toBytes()]));
})().catch(error => { console.error(error.message); process.exit(1); });
