// Executed by the isolated frontend/gRPC acceptance harness, with real SDK signatures.
const assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const { Ed25519Keypair } = appRequire('@mysten/sui/keypairs/ed25519');
const { MultiSigPublicKey, MultiSigSigner } = appRequire('@mysten/sui/multisig');
const { requestSuiFromFaucetV2 } = appRequire('@mysten/sui/faucet');
module.exports = async ({client, execute, operator, faucet}) => {
  assert.ok(['localhost','127.0.0.1','[::1]'].includes(new URL(faucet).hostname));
  const load = loader(), sui = load('lib/sui.ts'), desired = load('lib/desired-state.ts'), handover = load('lib/fleet-handover.ts');
  const key1=Ed25519Keypair.generate(), key2=Ed25519Keypair.generate();
  const multi=MultiSigPublicKey.fromPublicKeys({threshold:2,publicKeys:[{publicKey:key1.getPublicKey(),weight:1},{publicKey:key2.getPublicKey(),weight:1}]});
  for (const recipient of [Ed25519Keypair.generate(),new MultiSigSigner(multi,[key1,key2])]) {
    const address=recipient.toSuiAddress(), source=operator.toSuiAddress();
    await requestSuiFromFaucetV2({host:faucet,recipient:address});
    await execute(sui.buildRegisterDeviceTx('handover-device',key1.toSuiAddress(),'arm',source));
    const device=(await sui.fetchDevices(client,source)).find(d=>d.name==='handover-device');
    assert.ok(device?.ownerCapId);
    await execute(sui.buildCreateGroupTx('handover-group','authority acceptance'));
    const group=(await sui.fetchGroups(client,source)).find(g=>g.name==='handover-group');
    await execute(sui.buildAddDeviceToGroupTx(group.objectId,device));
    await execute(desired.buildDesiredRevisionTx([group.objectId],{componentName:'handover-app',version:'1.0.0',recipeBlobId:'recipe',artifactSha256:'ab'.repeat(32),recipeSha256:'cd'.repeat(32),initiallyPaused:true}));
    const state=(await desired.fetchDesiredStates(client,[group.objectId]))[group.objectId];
    const record=state.components['handover-app'];
    const inventory=[{objectId:device.ownerCapId,kind:0},{objectId:group.objectId,kind:1},{objectId:record,kind:2}];
    const proposed=await execute(handover.buildProposeHandoverTx(address,inventory,BigInt(Date.now()+3600000)));
    const id=Object.keys(proposed.objectTypes).find(id=>proposed.objectTypes[id].endsWith('::fleet_handover::Handover'));
    assert.ok(id);
    assert.equal((await handover.readHandover(client,id)).state,0);
    await execute(handover.buildHandoverActionTx(id,'confirm'),recipient);
    // An interruption between these transactions leaves durable progress.
    await execute(handover.buildHandoverStepsTx(id,inventory.slice(0,1)));
    assert.deepEqual((await handover.readHandover(client,id)).completed,[true,false,false]);
    await execute(handover.buildHandoverStepsTx(id,inventory.slice(1)));
    await execute(handover.buildHandoverActionTx(id,'finish'),recipient);
    assert.equal((await handover.readHandover(client,id)).state,3);
    assert.ok((await sui.fetchDevices(client,address)).some(d=>d.objectId===device.objectId && d.deviceAddress===key1.toSuiAddress()));
    assert.ok(!(await sui.fetchDevices(client,source)).some(d=>d.objectId===device.objectId));
    assert.ok((await sui.fetchGroups(client,address)).some(g=>g.objectId===group.objectId));
    assert.ok(!(await sui.fetchGroups(client,source)).some(g=>g.objectId===group.objectId));
    assert.ok((await sui.fetchDeploymentEvents(client,address)).some(d=>d.deploymentId===record));
    assert.ok(!(await sui.fetchDeploymentEvents(client,source)).some(d=>d.deploymentId===record));
    assert.deepEqual((await desired.fetchDesiredStates(client,[group.objectId]))[group.objectId],state);
    await execute(desired.buildDesiredRevisionTx([group.objectId],undefined,['handover-app']),recipient);
    await execute(sui.buildDeleteDeploymentTx(record),recipient);
    await execute(sui.buildDeleteGroupTx(group.objectId),recipient);
    await execute(sui.buildDeregisterDeviceTx(device),recipient);
  }
  console.log('PASS: recipient-confirmed populated handover to ed25519 and 2-of-2 native multisig; persistent partial progress, current-owner inventory, desired-state/control authority and cleanup');
};
