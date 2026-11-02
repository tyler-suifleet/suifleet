// Read canonical current object bytes; never signs or reads keys.
const { appRequire, loader } = require('./frontend_test_utils.cjs');
const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
const { bcs } = appRequire('@mysten/sui/bcs');
const assert = require('node:assert/strict');
(async () => {
  const [rpc,customer,origin,operation,values] = process.argv.slice(2);
  const client=new SuiGrpcClient({network:'testnet',baseUrl:rpc});
  const object=(await client.getObject({objectId:customer,include:{content:true}})).object;
  assert.equal(object.type,origin+'::customer::Customer');
  const value=JSON.parse(values);
  const bytes=operation==='configure_security_delays'?bcs.vector(bcs.u64()).serialize(value):bcs.vector(bcs.u8()).serialize(value);
  const args=bcs.vector(bcs.vector(bcs.u8())).serialize([bcs.string().serialize(operation).toBytes(),bcs.Address.serialize(customer).toBytes(),bytes.toBytes()]).toBytes();
  process.stdout.write(JSON.stringify(Array.from(args)));
})().catch(e=>{console.error(e.message);process.exit(1)});
