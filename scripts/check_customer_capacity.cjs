// Read-only production console admission check against a resolved live manifest.
const fs=require("node:fs"),assert=require("node:assert/strict");
const input=JSON.parse(fs.readFileSync(0,"utf8"));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(input.manifest);
const {loader,appRequire}=require("./frontend_test_utils.cjs");
const {SuiGrpcClient}=appRequire("@mysten/sui/grpc"),{Transaction}=appRequire("@mysten/sui/transactions");
(async()=>{
 const e=loader()("lib/customer-entitlements.ts");
 const client=new SuiGrpcClient({network:input.manifest.network,baseUrl:input.manifest.rpc_url});
 const tx=new Transaction();tx.moveCall({target:`${input.manifest.call_package_id}::customer::register_device`,arguments:[tx.object(input.customerId)]});
 if(input.expectFull) await assert.rejects(()=>e.checkDeviceCapacity(client,tx),/Device limit reached:.*active devices, limit.*Device entitlements/);
 else await e.checkDeviceCapacity(client,tx);
 console.log(`PASS: production console pre-sign capacity ${input.expectFull?"refusal":"allowance"} on testnet`);
})().catch(e=>{console.error(e);process.exitCode=1});
