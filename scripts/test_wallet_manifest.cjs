// Exercise the actual wallet hook's network/signing/effects boundary.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const ts = require("../dapp/node_modules/typescript");
const Module = require("node:module");
const fixture = JSON.parse(fs.readFileSync(path.resolve(__dirname,"../manifests/fixture.localnet.json"),"utf8"));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(fixture);
function load(relative, dependencies = {}) {
  const filename=path.resolve(__dirname,relative);
  const loaded=new Module(filename,module); loaded.filename=filename; loaded.paths=Module._nodeModulePaths(path.dirname(filename));
  const original=loaded.require.bind(loaded);
  loaded.require=(name) => Object.hasOwn(dependencies,name) ? dependencies[name] : original(name);
  loaded._compile(ts.transpileModule(fs.readFileSync(filename,"utf8"), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020,esModuleInterop:true}}).outputText,filename);
  return loaded.exports;
}
const manifest = load("../dapp/src/lib/deployment-manifest.ts");
let chain=fixture.chain_identifier, execution="success", signingError, signs=0;
const client={
  getChainIdentifier:async()=>chain,
  getObject:async({id}) => id === fixture.call_package_id ? {data:{version:fixture.package_version,bcs:{dataType:"package",typeOriginTable:Object.entries(fixture.type_origins).map(([name,origin])=>({module_name:name.split("::")[0],datatype_name:name.split("::")[1],package:origin}))}}} :
    id === fixture.upgrade_cap_id ? {data:{content:{dataType:"moveObject",type:"0x2::package::UpgradeCap",fields:{package:fixture.call_package_id,version:fixture.package_version}}}} :
    {data:{content:{dataType:"moveObject",type:`${fixture.type_origins["device_registry::DeviceRegistry"]}::device_registry::DeviceRegistry`},owner:{Shared:{initial_shared_version:"1"}}}},
  waitForTransaction:async()=>({effects:{status:{status:execution,error:execution==="failure"?"Move abort":undefined}}}),
};
const hook=load("../dapp/src/hooks/useFleetTransaction.ts",{
  "@/lib/deployment-manifest":manifest,
  "@tanstack/react-query":{useMutation:(options)=>options},
  "@mysten/dapp-kit":{useSuiClient:()=>client,useSignAndExecuteTransaction:()=>({mutateAsync:async(args)=>{
    signs++; assert.equal(args.chain,`sui:${fixture.network}`);
    if(signingError) throw new Error(signingError);
    return {digest:"test-digest"};
  }})},
});
(async()=>{
  const mutate=hook.useFleetTransaction().mutationFn;
  assert.equal((await mutate({transaction:{}})).digest,"test-digest");
  assert.equal(signs,1);
  chain="deadbeef"; await assert.rejects(mutate({transaction:{}}),/chain identifier/); assert.equal(signs,1);
  chain=fixture.chain_identifier;
  signingError="Wallet disconnected"; await assert.rejects(mutate({transaction:{}}),/Wallet disconnected/);
  signingError="User rejected signing"; await assert.rejects(mutate({transaction:{}}),/User rejected signing/);
  signingError=undefined; execution="failure"; await assert.rejects(mutate({transaction:{}}),/Move abort/);
  console.log("PASS: wallet hook pins network, blocks stale chain before signing, propagates disconnect/rejection, verifies execution effects");
})().catch(error=>{console.error(error);process.exitCode=1;});
