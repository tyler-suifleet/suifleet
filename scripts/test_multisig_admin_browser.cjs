const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
const {appRequire}=require('./frontend_test_utils.cjs'),{build}=appRequire('esbuild'),{chromium}=appRequire('playwright');
(async()=>{
 const root=path.resolve(__dirname,'..'),entry=path.join(root,'scripts/browser/multisig-fixture.jsx'),m=JSON.parse(await fs.readFile('manifests/fixture.localnet.json'));
 const result=await build({entryPoints:[entry],bundle:true,write:false,platform:'browser',jsx:'automatic',nodePaths:[path.join(root,'dapp/node_modules')],alias:{'@':path.join(root,'dapp/src')},plugins:[{name:'fixture',setup(b){b.onResolve({filter:/^(@mysten\/dapp-kit-react|@\/hooks\/useFleetTransaction)$/},()=>({path:entry}));}}],define:{'process.env':'{}','process.env.NODE_ENV':JSON.stringify('production'),'process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST':JSON.stringify(JSON.stringify(m))}});
 const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
 try{
 const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://fleet.test/**',r=>r.fulfill({contentType:'text/html',body:'<div id=root></div>'}));await page.goto('http://fleet.test');await page.addScriptTag({content:result.outputFiles[0].text});
 await page.getByLabel('Ordered public keys, weights and threshold (JSON)').fill(await page.evaluate(()=>JSON.stringify(window.multisig.config)));
 await page.getByText('Threshold: 2.',{exact:false}).waitFor();assert.equal(await page.locator('li').count(),3);
 await page.getByLabel('Export/import public transaction bundle').fill(await page.evaluate(()=>window.multisig.bundle));
 await page.evaluate(()=>{window.multisig.state.account={address:'0xb',chains:['sui:localnet']}});
 await page.getByRole('button',{name:'Sign exact bytes with member wallet'}).click();await page.getByText('Connected wallet is not a member',{exact:true}).waitFor();assert.equal(await page.evaluate(()=>window.multisig.state.signs),0);
 const {Ed25519Keypair}=appRequire('@mysten/sui/keypairs/ed25519'),{fromBase64}=appRequire('@mysten/sui/utils');const bundle=JSON.parse(await page.evaluate(()=>window.multisig.bundle));
 const key=Ed25519Keypair.fromSecretKey(new Uint8Array(32).fill(1));
 await page.evaluate(address=>{window.multisig.state.account={address,chains:['sui:localnet']};window.multisig.state.wrongBytes=true},key.toSuiAddress());
 await page.getByRole('button',{name:'Sign exact bytes with member wallet'}).click();await page.getByText('Wallet changed the transaction bytes',{exact:true}).waitFor();
 await page.evaluate(()=>{window.multisig.state.wrongBytes=false});await page.getByRole('button',{name:'Sign exact bytes with member wallet'}).click();
 await page.waitForFunction(()=>JSON.parse(document.querySelector('textarea[rows="7"]').value).signatures.length===1);
 const signed=(await key.signTransaction(fromBase64(bundle.bytes))).signature;await page.getByLabel('Export/import one partial signature').fill(signed);
 await page.getByRole('button',{name:'Verify and add partial signature'}).click();await page.getByText('Duplicate member signature',{exact:true}).waitFor();assert.deepEqual(errors,[]);
 console.log('PASS actual Chromium multisig component: members/threshold, nonmember refusal, changed-wallet bytes rejected, member signing and duplicate partial refusal (fixture wallet/transport)');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
