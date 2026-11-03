// Public object bytes only; canonical existing wrapper arguments.
const { appRequire } = require('./frontend_test_utils.cjs');
const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
const { bcs } = appRequire('@mysten/sui/bcs');
(async () => {
 const [rpc, customer, fleet, origin, name] = process.argv.slice(2);
 const client = new SuiGrpcClient({ network: 'testnet', baseUrl: rpc });
 const c = (await client.getObject({ objectId: customer, include: { content: true } })).object;
 if (c.type !== origin + '::customer::Customer') throw Error('customer origin mismatch');
 let values;
 if (name === '--enroll') values = [bcs.string().serialize('configure_second_person').toBytes(), bcs.Address.serialize(customer).toBytes()];
 else if (name) {
   const f = (await client.getObject({ objectId: fleet, include: { content: true } })).object;
   if (f.type !== origin + '::customer::Fleet') throw Error('fleet origin mismatch');
   values = [bcs.string().serialize('rename_fleet').toBytes(), bcs.Address.serialize(customer).toBytes(), bcs.Address.serialize(fleet).toBytes(), bcs.string().serialize(name).toBytes()];
 } else values = [bcs.string().serialize('set_security').toBytes(), bcs.Address.serialize(customer).toBytes(), bcs.vector(bcs.u8()).serialize([0,0,0,0,0,0,0]).toBytes()];
 process.stdout.write(JSON.stringify([...bcs.vector(bcs.vector(bcs.u8())).serialize(values).toBytes()]));
})().catch(e => { console.error(e.message); process.exitCode = 1; });
