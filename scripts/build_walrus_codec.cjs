#!/usr/bin/env node
// Bundle the pinned keyless SDK codec; no npm dependencies are needed at runtime.
const fs=require('node:fs/promises'),path=require('node:path'),crypto=require('node:crypto');
const {createRequire}=require('node:module');const root=path.resolve(__dirname,'..'),r=createRequire(path.join(root,'dapp/package.json'));
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
(async()=>{
 const output=path.resolve(process.argv[2] ?? 'dist/walrus-codec');await fs.mkdir(output,{recursive:true});
 const esbuild=r('esbuild');if(esbuild.version!=='0.28.2')throw Error('Unreviewed codec bundler');
 for(const [name,version] of [['@mysten/sui','2.36.0'],['@mysten/walrus','1.2.35']]){
  const pkg=JSON.parse(await fs.readFile(path.join(root,'dapp/node_modules',name,'package.json'),'utf8'));if(pkg.version!==version)throw Error('Unreviewed SDK version');
 }
 await esbuild.build({entryPoints:[path.join(root,'dapp/scripts/walrus-device-codec.mjs')],bundle:true,platform:'node',target:'node22',format:'cjs',
  outfile:path.join(output,'walrus-device-codec.cjs'),legalComments:'eof',logLevel:'warning'});
 const walrusRequire=createRequire(r.resolve('@mysten/walrus')),wasmRequire=createRequire(walrusRequire.resolve('@mysten/walrus-wasm'));
 const wasm=path.join(path.dirname(wasmRequire.resolve('@mysten/walrus-wasm')),'nodejs/walrus_wasm_bg.wasm');
 await fs.copyFile(wasm,path.join(output,'walrus_wasm_bg.wasm'));
 const files={};for(const name of ['walrus-device-codec.cjs','walrus_wasm_bg.wasm']){const b=await fs.readFile(path.join(output,name));files[name]={bytes:b.length,sha256:hash(b)};}
 await fs.writeFile(path.join(output,'manifest.json'),JSON.stringify({schema:1,node:'22.22.2',sui:'2.36.0',walrus:'1.2.35',esbuild:'0.28.2',files},null,2)+'\n');
 console.log(JSON.stringify({artifact:output,files}));
})().catch(e=>{console.error(e.message);process.exit(1)});
