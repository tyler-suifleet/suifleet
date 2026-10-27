const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {loader}=require('./frontend_test_utils.cjs');
const {selectManifest,compareVersions}=loader()('lib/component-selector.ts');
const cases=JSON.parse(fs.readFileSync(path.join(__dirname,'../interfaces/component-cases.json')));
for(const c of cases){
 const v=c.value, manifests=v.platform_os.map((os,i)=>({os,arch:v.platform_arch[i],recipeBlobId:v.recipe_blob_ids[i],recipeSha256:v.recipe_sha256s[i],artifactSha256:v.artifact_sha256s[i]}));
 let index=null;
 try {
  assert.equal(v.schema_version,'1');assert.ok([v.platform_arch,v.recipe_blob_ids,v.recipe_sha256s,v.artifact_sha256s].every(a=>a.length===manifests.length));
  index=manifests.indexOf(selectManifest(v.component_name,v.version,manifests,c.os,c.arch));
 }catch{}
 assert.equal(index,c.selected,c.name);
}
const ordered=['1.0.0-alpha','1.0.0-alpha.1','1.0.0-alpha.beta','1.0.0-beta','1.0.0-beta.2','1.0.0-beta.11','1.0.0-rc.1','1.0.0','2.0.0'];
assert.deepEqual([...ordered].reverse().sort(compareVersions),ordered);
assert.ok(compareVersions('1.0.0+aaa','1.0.0+bbb')<0);
console.log(`PASS: ${cases.length} independent catalog selection vectors and SemVer discovery ordering`);

const manifest=JSON.parse(fs.readFileSync(path.join(__dirname,'../manifests/fixture.localnet.json')));
for (const name of loader()('lib/protocol-version.ts').CATALOG_TYPES) manifest.type_origins[name]=manifest.call_package_id;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(manifest);
const catalog=loader()('lib/catalog.ts');
for (const fleet of [undefined, {customerId:'0x51',id:'0x52'}]) {
 const groups=['0x61','0x62'];
 const tx=catalog.buildCatalogDeploymentTx('0x41',groups,[],true,fleet,true).getData();
 const functions=tx.commands.map(c=>c.MoveCall.function);
 assert.equal(functions.filter(n=>n==='set_component').length,2);
 assert.equal(functions.filter(n=>n==='commit'||n==='commit_revision').length,2);
 assert.equal(functions.at(-1),'share_deployment');
 assert.ok(functions.includes('set_control'));
 assert.ok(functions.includes(fleet?'new_catalog_deployment':'new_deployment'));
}
assert.throws(()=>catalog.buildCatalogDeploymentTx('0x41',[],[],false,undefined,true),/groups only/);
assert.throws(()=>catalog.buildCatalogDeploymentTx('0x41',['0x61'],['0x71'],false,undefined,true),/groups only/);
assert.equal(catalog.buildCatalogDeploymentTx('0x41',['0x61'],['0x71']).getData().commands[0].MoveCall.function,'create_deployment');
console.log('PASS: catalog desired PTBs, both authority paths, atomic multi-group revisions, paused admission and one-shot fallback');
