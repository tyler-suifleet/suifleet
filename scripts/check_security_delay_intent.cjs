// Independently compare the production console's domain hash to chain Action BCS.
const fs=require('node:fs'),assert=require('node:assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const manifest=JSON.parse(fs.readFileSync('manifests/fixture.localnet.json'));
Object.assign(manifest,{chain_identifier:input.chain,call_package_id:input.head,type_origins:input.origins});
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(manifest);
const {loader}=require('./frontend_test_utils.cjs');
const hash=loader()('lib/security-delay.ts').securityIntent(input.customer,Uint8Array.from(input.arguments));
assert.deepEqual(hash,typeof input.intent === 'string' ? Array.from(Buffer.from(input.intent,'base64')) : input.intent);
console.log('PASS production console intent matches on-chain Action');
