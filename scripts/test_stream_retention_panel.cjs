// Real component + transaction builder. Controlled async chain/wallet fixtures
// exercise authority, malformed input and a late response after account change.
const assert = require('node:assert/strict');
const {loader,appRequire} = require('./frontend_test_utils.cjs');
const {normalizeSuiAddress:id} = appRequire('@mysten/sui/utils');
const config={packageId:id('0x82'),streamRetentionOrigin:id('0x83'),fleetId:id('0xf1'),budgetId:id('0xb1')};
const actual=loader()('lib/stream-retention.ts');
let slots=[],cursor=0,effects=[],account={address:id('0xa1')},policies=[3,13,26],signed=[],pendingResolve;
let selection={supported:false,identity:'legacy'},admin=true;
const client={};
const react={
 useState(initial){const i=cursor++;if(!slots[i])slots[i]={value:initial};return [slots[i].value,v=>{slots[i].value=typeof v==='function'?v(slots[i].value):v;}];},
 useRef(initial){const i=cursor++;if(!slots[i])slots[i]={current:initial};return slots[i];},
 useEffect(fn,deps){const i=cursor++;const prev=slots[i];if(!prev || deps.some((d,j)=>d!==prev.deps[j])){effects.push(()=>{prev?.cleanup?.();slots[i]={deps,cleanup:fn()};});}},
};
const dependencies={react,
 '@mysten/dapp-kit-react':{useCurrentClient:()=>client,useCurrentAccount:()=>account},
 '@/lib/customer-fleets':{fetchCustomerFleets:async()=>[{...selection.fleet,administrator:admin}]},
 '@/lib/data-authority':{selectedDataAuthority:loader()('lib/data-authority.ts').selectedDataAuthority},
 '@/hooks/useFleetDataConfig':{useFleetDataConfig:()=>config},
 '@/hooks/useFleetSelection':{useFleetSelection:()=>selection},
 '@/lib/fleet-budget':{storageConfig:()=>({}),fetchManagedBudget:async()=>({id:config.budgetId,capId:id('0xc2')}),fetchBudgets:async()=>account.address===id('0xa1')?[{id:config.budgetId,capId:id('0xc1')}]:[]},
 '@/lib/stream-retention':{...actual,readStreamPolicies:async()=>policies},
 '@/hooks/useFleetTransaction':{useFleetTransaction:()=>({isPending:false,mutateAsync:({transaction})=>{signed.push(transaction);return new Promise(resolve=>{pendingResolve=resolve;});}})},
};
const fs=require('node:fs'),path=require('node:path'),Module=require('node:module'),ts=appRequire('typescript');
const filename=path.resolve(__dirname,'../dapp/src/components/StreamRetention.tsx'),compiled=new Module(filename,module);
compiled.filename=filename;compiled.require=name=>Object.hasOwn(dependencies,name)?dependencies[name]:appRequire(name);
compiled._compile(ts.transpileModule(fs.readFileSync(filename,'utf8'),{fileName:filename,compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,filename);
const {StreamRetentionPanel}=compiled.exports;
function render(){cursor=0;effects=[];const tree=StreamRetentionPanel();effects.forEach(f=>f());return tree;}
function nodes(tree){if(Array.isArray(tree))return tree.flatMap(nodes);if(!tree||typeof tree!=='object')return [];return [tree,...nodes(tree.props?.children)];}
const text=t=>typeof t==='string'||typeof t==='number'?String(t):Array.isArray(t)?t.map(text).join(''):t?.props?text(t.props.children):'';
const button=t=>nodes(t).find(n=>n.type==='button');
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{
 render();await flush();let tree=render();assert.equal(button(tree).props.disabled,false);assert.match(text(tree),/Logs: 3 epochs/);
 const technical=nodes(tree).find(n=>n.type==='details');assert(technical,'technical controls require disclosure');assert.match(text(technical),/Archive storage object ID/);assert.match(text(tree),/WAL, the asset used to buy storage/);assert.match(text(tree),/Expired data cannot be restored/);
 nodes(tree).find(n=>n.type==='input').props.onChange({target:{value:'54'}});tree=render();await button(tree).props.onClick();await flush();assert.equal(signed.length,0,'invalid horizon never reaches wallet');
 nodes(tree).find(n=>n.type==='input').props.onChange({target:{value:'26'}});tree=render();button(tree).props.onClick();await flush();assert.equal(signed.length,1);
 const tx=signed[0].getData();assert.equal(tx.commands[0].MoveCall.function,'set_stream_retention');
 account={address:id('0xa2')};render();await flush();tree=render();assert.equal(button(tree).props.disabled,true,'support/non-owner cannot save');
 pendingResolve({digest:'fixture'});await flush();tree=render();assert(!text(tree).includes('Retention confirmed'),'old-account response must not publish success');
 selection={supported:true,identity:'managed-1',fleet:{id:id('0xf9'),customerId:id('0xc9'),budgetId:config.budgetId}};
 render();await flush();tree=render();assert.equal(button(tree).props.disabled,false,'fresh managed administrator can save using custodied cap');
 button(tree).props.onClick();await flush();assert.equal(signed.length,2);
 assert(signed[1].getData().inputs.some(i=>i.UnresolvedObject?.objectId===id('0xc2')),'builder uses managed budget cap for transaction wrapper');
 admin=false;selection={...selection,identity:'managed-read-only'};render();await flush();tree=render();assert.equal(button(tree).props.disabled,true,'managed read-only credential cannot save');
 pendingResolve({digest:'fixture'});await flush();assert(!text(render()).includes('Retention confirmed'),'late managed authority response discarded');
 console.log('stream policy panel: owner edit, non-owner refusal, horizon validation and account-change response isolation PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
