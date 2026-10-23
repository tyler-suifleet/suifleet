const {writeReceipt}=require('./pilot_receipt.cjs');
// Real FleetDataPage in Chromium with real BCS index parsing; no wallet/provider claim.
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),http=require('node:http'),assert=require('node:assert/strict');
const {performance}=require('node:perf_hooks'),{execFileSync}=require('node:child_process');
const {appRequire}=require('./frontend_test_utils.cjs');
const {build}=appRequire('esbuild'),{chromium}=appRequire('playwright'),{normalizeSuiAddress:id}=appRequire('@mysten/sui/utils');
(async()=>{
 await fs.rm(process.argv[2]||'/tmp/sf090-browser.json',{force:true});
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'sf090-browser-'));let server,browser;
 try {
  const manifest=JSON.parse(await fs.readFile('manifests/fixture.localnet.json'));manifest.chain_identifier='4c78adac';manifest.shared_objects.registry.id=id('0xa1');
  const config={schema:1,chainIdentifier:'4c78adac',registryId:id('0xa1'),packageId:id('0xa2'),typeOrigin:id('0xa2'),fleetId:id('0xf1'),budgetId:id('0xb1'),storageId:id('0xb2')};
  const entry=path.resolve('scripts/browser/pilot-acceptance.jsx');
  await build({entryPoints:[entry],outfile:path.join(dir,'app.js'),bundle:true,platform:'browser',jsx:'automatic',alias:{'@':path.resolve('dapp/src')},nodePaths:[path.resolve('dapp/node_modules')],
   plugins:[{name:'pilot-wallet-hooks',setup(b){b.onResolve({filter:/^(@mysten\/dapp-kit-react|@\/hooks\/(useDevices|useFleetTransaction))$/},()=>({path:entry}));}}],
   define:{'process.env':'{}','process.env.NODE_ENV':'"development"','process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST':JSON.stringify(JSON.stringify(manifest)),'process.env.NEXT_PUBLIC_FLEET_DATA':JSON.stringify(JSON.stringify(config))},logLevel:'warning'});
  server=http.createServer(async(req,res)=>{res.setHeader('Content-Type',req.url==='/app.js'?'text/javascript':'text/html');res.end(req.url==='/app.js'?await fs.readFile(path.join(dir,'app.js')):'<!doctype html><div id="root"></div><script src="/app.js"></script>');});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||undefined,headless:true,args:['--no-sandbox']});
  const page=await browser.newPage(),errors=[];page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
  const cdp=await page.context().newCDPSession(page);await cdp.send('Performance.enable');
  const timings=[];
  for(let round=0;round<5;round++) {
   const start=performance.now();await page.goto(`http://127.0.0.1:${server.address().port}`);
   await page.waitForFunction(()=>document.querySelector('select')?.options.length===101); // includes placeholder
   const initial=performance.now()-start;
   await page.evaluate(()=>window.pilotAcceptance.fail(true));
   await page.getByRole('button',{name:'Load more devices'}).click();await page.getByRole('status').filter({hasText:'Fleet index page unavailable.'}).waitFor();
   assert.equal(await page.locator('select option').count(),101,'outage must not fabricate more devices');
   await page.evaluate(()=>window.pilotAcceptance.fail(false));
   const moreStart=performance.now();await page.getByRole('button',{name:'Load more devices'}).click();
   await page.waitForFunction(()=>document.querySelector('select')?.options.length===129);
   assert.equal(await page.getByRole('button',{name:'Load more devices'}).count(),0);
   assert.equal(new Set(await page.locator('select option').evaluateAll(options=>options.map(o=>o.value))).size,129);
   const metrics=(await cdp.send('Performance.getMetrics')).metrics;
   timings.push({initial_ms:initial,page_recovery_ms:performance.now()-moreStart,js_heap_used_bytes:metrics.find(m=>m.name==='JSHeapUsedSize').value,dom_nodes:metrics.find(m=>m.name==='Nodes').value});
  }
  assert.deepEqual(errors,[]);
  const {distribution}=require('./pilot_load.cjs'),measurements={initial:distribution(timings.map(t=>t.initial_ms)),page_recovery:distribution(timings.map(t=>t.page_recovery_ms))};
  assert(measurements.initial.p95_ms<5000);assert(measurements.page_recovery.p95_ms<1000);
  const receipt={schema:1,result:'PASS',mode:'real-chromium-fixture-transport',revision:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),browser:browser.version(),devices:128,rounds:5,measurements,timings,checks:['real FleetDataPage + BCS index verification','100 to 128 devices without duplicates','RPC outage does not fabricate devices','page recovery after throttling'],limits:['fixture chain and wallet hooks','no real extension/session/provider authorization','pagination only; no encrypted archive rendering load','renderer JS heap, not complete browser/host RSS']};
  await writeReceipt(process.argv[2]||'/tmp/sf090-browser.json',receipt);console.log(JSON.stringify(receipt));
 }finally{if(browser)await browser.close();if(server)await new Promise(resolve=>server.close(resolve));await fs.rm(dir,{recursive:true,force:true});}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
