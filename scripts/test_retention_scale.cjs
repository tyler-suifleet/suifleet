// Invoked only by the network-none upgrade harness: real Walrus registration,
// real per-blob prepayments, and effects-derived gas, never synthetic counters.
const assert = require('node:assert/strict');
module.exports = async ({client,signer,Transaction,execute,current,budget,ownerCap,system,walCoin,walrusPackage,created}) => {
  let tx=new Transaction();
  const [quota]=tx.moveCall({target:`${current}::fleet_storage::new_quota`,arguments:[tx.pure.u64(1000),tx.pure.u64(1e12),tx.pure.u64(20000),tx.pure.u64(100),tx.pure.u64(1e12)]});
  tx.moveCall({target:`${current}::fleet_storage::create`,arguments:[tx.object(budget),tx.object(ownerCap),quota]});
  const storage=created(await execute('scale create storage',tx),'::fleet_storage::FleetStorage');
  tx=new Transaction();
  const [spendable,protectedWal]=tx.splitCoins(tx.object(walCoin),[tx.pure.u64(1e12),tx.pure.u64(1e12)]);
  const [zeroGas]=tx.splitCoins(tx.gas,[tx.pure.u64(0)]);
  const args=()=>[tx.object(storage),tx.object(budget),tx.object(ownerCap)];
  tx.moveCall({target:`${current}::fleet_storage::deposit_wal`,arguments:[...args(),spendable]});
  tx.moveCall({target:`${current}::fleet_storage::buy_storage`,arguments:[...args(),tx.object(system),tx.pure.u64(1e12),tx.pure.u32(5)]});
  tx.moveCall({target:`${current}::fleet_storage::configure_retention`,arguments:[...args(),tx.pure.address(signer.toSuiAddress()),tx.pure.u32(2),tx.pure.u64(1e12),tx.pure.u64(0),tx.pure.u64(0)]});
  tx.moveCall({target:`${current}::fleet_storage::fund_retention`,arguments:[tx.object(storage),protectedWal,zeroGas]});
  await execute('scale fund real pool and prepaid renewal reserves',tx);
  let count=0;
  function registration() {
    const root=10000+count++;
    const [id]=tx.moveCall({target:`${walrusPackage}::blob::derive_blob_id`,arguments:[tx.pure.u256(root),tx.pure.u8(1),tx.pure.u64(10)]});
    tx.moveCall({target:`${current}::fleet_storage::register_retained_fleet_blob`,arguments:[...args(),tx.object(system),id,tx.pure.u256(root),tx.pure.u64(10),tx.pure.u8(1),tx.object('0x6'),tx.pure.u32(7),tx.pure.u64(100)]});
  }
  const measurements=[];
  for (const size of [10,100,1000,10000]) {
    while(count<size) {
      tx=new Transaction();
      for(let i=0,n=Math.min(50,size-count);i<n;i++) registration();
      await execute(`scale seed through ${count} protected blobs`,tx);
    }
    tx=new Transaction();registration();
    const r=await execute(`measure registration with ${size} protected blobs`,tx);
    measurements.push({protected_blobs:size,digest:r.digest,gas:r.effects.gasUsed});
    console.log(JSON.stringify({scale_measurement:measurements.at(-1)}));
  }
  assert.ok(measurements.every(m=>m.gas),'gas effects are required');
  const computation=measurements.map(m=>BigInt(m.gas.computationCost));
  assert.equal(new Set(computation.map(String)).size,1,'registration computation must stay flat');
  const storageCosts=measurements.map(m=>BigInt(m.gas.storageCost));
  assert.equal(new Set(storageCosts.map(String)).size,1,'per-registration object growth must stay flat');
  console.log(JSON.stringify({check:'SF128 real protected registration gas from 10 through 10000',measurements}));
};
