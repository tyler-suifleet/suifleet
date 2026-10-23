// Exact pinned Site ABI and byte-range resources. No wallet or transport here.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),crypto=require('node:crypto');
const {appRequire}=require('./frontend_test_utils.cjs'),{bcs}=appRequire('@mysten/sui/bcs');
const pin=require('../tools/walrus-sites/site-abi.json');
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
const hashInt=b=>bcs.u256().parse(Buffer.from(sha(b),'hex'));
async function verifyAbi(client){
 for(const [label,expected] of Object.entries(pin.types)){const [moduleName,name]=label.split('::');const actual=(await client.movePackageService.getDatatype({packageId:pin.packageId,moduleName,name})).response.datatype;assert.deepEqual(JSON.parse(JSON.stringify(actual)),expected,`Site datatype drift: ${label}`);}
 for(const [label,expected] of Object.entries(pin.functions)){const [moduleName,name]=label.split('::');assert.deepEqual(JSON.parse(JSON.stringify((await client.getMoveFunction({packageId:pin.packageId,moduleName,name})).function)),expected,`Site call drift: ${label}`);}
}
async function pack(directory,files){
 const root=path.resolve(directory),chunks=[],resources=[];let offset=0;
 for(const [name,expected] of Object.entries(files).sort(([a],[b])=>a<b?-1:a>b?1:0)){
  assert(name.startsWith('/') && !name.includes('..') && !name.includes('\\'),'unsafe resource path');
  const file=path.resolve(root,'.'+name);assert(file.startsWith(root+path.sep));assert(!(await fs.lstat(file)).isSymbolicLink());assert((await fs.realpath(file)).startsWith((await fs.realpath(root))+path.sep),'resource escapes export');const bytes=await fs.readFile(file);
  assert.equal(bytes.length,expected.bytes,`changed resource size ${name}`);assert.equal(sha(bytes),expected.sha256,`changed resource ${name}`);
  assert(bytes.length>0,'empty resources need a separate encoding policy');assert(offset+bytes.length<=64*1024*1024,'Site packed blob exceeds device-independent publication limit');
  resources.push({path:name,start:offset,end:offset+bytes.length-1,sha256:expected.sha256,hash:hashInt(bytes)});chunks.push(bytes);offset+=bytes.length;
 }
 return {bytes:Buffer.concat(chunks),resources};
}
function reserve(limit,used,budget){
 assert([limit,used,budget].every(Number.isSafeInteger) && used>=0 && budget>0 && budget<=500000000 && used+budget<=limit,'publisher non-replenishing allowance');return used+budget;
}
function settle(used,reserved,gas){
 for(const field of ['computationCost','storageCost','storageRebate'])assert(/^\d+$/.test(String(gas?.[field])),'missing publisher gas effects');
 const raw=BigInt(gas.computationCost)+BigInt(gas.storageCost)-BigInt(gas.storageRebate),cost=raw>0n?raw:0n;
 assert(cost<=BigInt(reserved),'publisher gas exceeds reservation');return {used:used-reserved+Number(cost),cost:Number(cost)};
}
const VecMap=bcs.struct('VecMap',{contents:bcs.vector(bcs.struct('Entry',{key:bcs.string(),value:bcs.string()}))});
const Range=bcs.struct('Range',{start:bcs.option(bcs.u64()),end:bcs.option(bcs.u64())});
const Resource=bcs.struct('Resource',{path:bcs.string(),headers:VecMap,blob_id:bcs.u256(),blob_hash:bcs.u256(),range:bcs.option(Range)});
const Routes=bcs.struct('Routes',{route_list:VecMap});
module.exports={pin,sha,hashInt,verifyAbi,pack,reserve,settle,Resource,Routes};
