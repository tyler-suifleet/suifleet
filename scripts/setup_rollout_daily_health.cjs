// Test-only real daily-cadence fleet-data fixture for SF-138. No public network,
// HTTP telemetry service, Walrus service or production credentials are used.
const fs = require('node:fs/promises'), path = require('node:path'), assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { appRequire } = require('./frontend_test_utils.cjs');
const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
const { Transaction } = appRequire('@mysten/sui/transactions');
const { Ed25519Keypair } = appRequire('@mysten/sui/keypairs/ed25519');
const { fromBase58, toHex } = appRequire('@mysten/sui/utils');
(async () => {
  assert.deepEqual((await fs.readdir('/sys/class/net')).sort(), ['lo']);
  assert.ok(await fs.stat('/.dockerenv'));
  const input = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
  const client = new SuiGrpcClient({ network: 'localnet', baseUrl: 'http://127.0.0.1:9000' });
  const chain = toHex(fromBase58((await client.getChainIdentifier()).chainIdentifier).slice(0,4));
  assert.equal(chain, input.chain);
  // Read only the explicitly supplied disposable CLI wallet. Never serialize it.
  const entries = JSON.parse(await fs.readFile(path.join(input.wallet, 'sui.keystore'), 'utf8'));
  const owner = entries.map(entry => Buffer.from(entry, 'base64')).filter(b => b.length === 33 && b[0] === 0)
    .map(b => Ed25519Keypair.fromSecretKey(b.subarray(1))).find(key => key.toSuiAddress() === input.owner);
  assert.ok(owner, 'disposable owner key missing');
  const tree = input.package, deps = path.join(input.runtime, 'daily-deps');
  await fs.mkdir(deps);
  const cache = '/home/dev/.move/git/https___github_com_MystenLabs_walrus_git_c68b530e0b1e522b4a3896190d883e4312a1cf0e/testnet-contracts';
  for (const name of ['wal','walrus']) await fs.cp(path.join(cache,name),path.join(deps,name),
    {recursive:true,filter:p=>!['build','Published.toml'].includes(path.basename(p))});
  const directories = [tree, ...['wal','walrus'].map(n=>path.join(deps,n)), ...['fleet_storage','fleet_data'].map(n=>path.join(tree,n))];
  for (const directory of directories) {
    const file = path.join(directory,'Move.toml');
    let text = (await fs.readFile(file,'utf8')).replaceAll('rev = "testnet-v1.82.1"','rev = "987123842623d6cb0e772dc665789cfd69bdec9f"');
    text = text.includes('[environments]') ? text.replace('[environments]',`[environments]\nsf138 = "${chain}"`) : text+`\n[environments]\nsf138 = "${chain}"\n`;
    for (const name of ['wal','walrus']) text=text.replace(new RegExp(`^${name} = \\{ git = [^\\n]+$`,'m'),`${name} = { local = "${deps}/${name}" }`);
    await fs.writeFile(file,text);
  }
  const published = async (directory,pkg,original,version,cap) => fs.writeFile(path.join(directory,'Published.toml'),
    `[published.sf138]\nchain-id = "${chain}"\npublished-at = "${pkg}"\noriginal-id = "${original}"\nversion = ${version}\ntoolchain-version = "1.81.1"\nbuild-config = { flavor = "sui", edition = "2024" }\nupgrade-capability = "${cap}"\n`);
  await published(tree,input.core,input.coreOrigin,2,input.upgradeCap);
  const compiler = path.join(input.runtime,'daily-compiler'); await fs.mkdir(compiler);
  await fs.writeFile(path.join(compiler,'sui.keystore'),'[]');
  await fs.writeFile(path.join(compiler,'client.yaml'),`keystore:\n  File: ${compiler}/sui.keystore\nenvs:\n- alias: localnet\n  rpc: http://127.0.0.1:9000\nactive_env: localnet\nactive_address: null\n`);
  const digests=[];
  async function execute(tx) {
    const result = await client.signAndExecuteTransaction({transaction:tx,signer:owner,include:{effects:true,objectTypes:true}});
    assert.ok(result.Transaction?.status.success, 'daily fixture transaction effects failed');
    await client.waitForTransaction({digest:result.Transaction.digest});
    digests.push(result.Transaction.digest); return result.Transaction;
  }
  const created = (r,suffix) => Object.keys(r.objectTypes).find(id=>r.objectTypes[id].endsWith(suffix));
  async function publish(directory) {
    let output;
    try { output=execFileSync('sui',['move','build','--path',directory,'--build-env','sf138','--no-tree-shaking','--dump-bytecode-as-base64'],
      {env:{...process.env,SUI_CONFIG_DIR:compiler},encoding:'utf8',maxBuffer:30*1024*1024,stdio:['ignore','pipe','pipe']}); }
    catch(error) { throw Error('daily fixture compilation failed: '+String(error.stderr).slice(-1500)); }
    const tx=new Transaction(),[cap]=tx.publish(JSON.parse(output.slice(output.indexOf('{')))); tx.transferObjects([cap],owner.toSuiAddress());
    const r=await execute(tx),pkg=r.effects.changedObjects.find(o=>o.outputState==='PackageWrite').objectId;
    await published(directory,pkg,pkg,1,created(r,'::package::UpgradeCap')); return pkg;
  }
  for (const name of ['wal','walrus']) await publish(path.join(deps,name));
  const storagePackage=await publish(path.join(tree,'fleet_storage')), dataPackage=await publish(path.join(tree,'fleet_data'));
  let tx=new Transaction(); const [deposit]=tx.splitCoins(tx.gas,[tx.pure.u64(100000000)]);
  const [limits]=tx.moveCall({target:`${input.core}::fleet_budget::new_limits`,arguments:[10000000,86400000,10000000,2,0].map(n=>tx.pure.u64(n))});
  const [cap]=tx.moveCall({target:`${input.core}::fleet_budget::create`,arguments:[tx.object(input.registry),deposit,limits,tx.pure.u64(0)]});
  tx.transferObjects([cap],owner.toSuiAddress()); let r=await execute(tx);
  const budget=created(r,'::fleet_budget::FleetBudget'),budgetCap=created(r,'::fleet_budget::BudgetOwnerCap');
  tx=new Transaction();tx.moveCall({target:`${input.core}::fleet_budget::enroll_device`,arguments:[tx.object(budget),tx.object(budgetCap),tx.object(input.registry),tx.object(input.ownerCap),tx.pure.u64(0),tx.pure.u64(0),tx.object('0x6')]});
  const account=created(await execute(tx),'::fleet_budget::GasAccount');
  tx=new Transaction();const [quota]=tx.moveCall({target:`${storagePackage}::fleet_storage::new_quota`,arguments:[86400000,1000000,2,1000000,1000000].map(n=>tx.pure.u64(n))});
  tx.moveCall({target:`${storagePackage}::fleet_storage::create`,arguments:[tx.object(budget),tx.object(budgetCap),quota]});
  const storage=created(await execute(tx),'::fleet_storage::FleetStorage');
  tx=new Transaction();tx.moveCall({target:`${dataPackage}::fleet_data::create`,arguments:[tx.object(budget),tx.object(budgetCap),tx.object(storage),tx.pure.string(chain)]});
  const fleet=created(await execute(tx),'::fleet_data::FleetData');
  tx=new Transaction();tx.moveCall({target:`${dataPackage}::fleet_data::configure`,arguments:[tx.object(fleet),tx.object(budget),tx.object(budgetCap),tx.pure.u64(86400000),tx.pure.u64(86400000),tx.pure.u64(300000)]});
  tx.moveCall({target:`${dataPackage}::fleet_data::enroll`,arguments:[tx.object(fleet),tx.object(budget),tx.object(budgetCap),tx.object(account)]});
  const writer=created(await execute(tx),'::fleet_data::DeviceData');
  console.log(JSON.stringify({digests,package_id:dataPackage,type_origin:dataPackage,fleet_id:fleet,budget_id:budget,storage_id:storage,writer_id:writer,account_id:account}));
})().catch(error=>{console.error(error.message);process.exitCode=1;});
