// No listener/server: Playwright fulfills a synthetic HTTPS page and customer PUTs.
const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs/promises');
const {appRequire}=require('./frontend_test_utils.cjs');
const {build}=appRequire('esbuild'),{chromium}=appRequire('playwright');
(async()=>{
 const fixture=await require('./fixtures/data-browser.cjs')();
 const {outputFiles}=await build({entryPoints:[path.resolve(__dirname,'browser/data-acceptance.jsx')],bundle:true,platform:'browser',write:false,jsx:'automatic',banner:{js:'var process = {env:{}};'},
  nodePaths:[path.resolve(__dirname,'../dapp/node_modules')],alias:{'@':path.resolve(__dirname,'../dapp/src')},
  define:{DATA_FIXTURE:JSON.stringify(fixture),'process.env.NODE_ENV':'"development"'},logLevel:'warning'});
 const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
 try{
  const page=await browser.newPage(),errors=[],uploads=[];let refuseUpload=false;page.setDefaultTimeout(10000);
  page.on('pageerror',e=>{errors.push(e.message);console.error('Browser fixture error:',e.message);});
  await page.route('https://data-fixture.invalid/**',route=>route.fulfill({contentType:'text/html',body:'<!doctype html><div id="root"></div><script src="https://bundle-fixture.invalid/app.js"></script>'}));
  await page.route('https://bundle-fixture.invalid/app.js',route=>route.fulfill({contentType:'text/javascript',body:outputFiles[0].text}));
  await page.route('https://customer.invalid/**',async route=>{uploads.push({url:route.request().url(),headers:route.request().headers(),body:route.request().postDataBuffer().toString()});await route.fulfill({status:refuseUpload?403:200,headers:{'Access-Control-Allow-Origin':'*'}});});
  await page.goto('https://data-fixture.invalid/');
  await page.getByRole('button',{name:'Browse fleet data',exact:true}).click();
  await page.getByRole('button',{name:'reading.json · sensor',exact:true}).waitFor();
  assert.equal(await page.locator('li').count(),4);
  await page.getByLabel('Group',{exact:true}).selectOption(fixture.groups[0].objectId);assert.equal(await page.locator('li').count(),3);
  await page.getByLabel('Stream',{exact:true}).selectOption('sensor');assert.equal(await page.locator('li').count(),1);
  await page.getByLabel('Key',{exact:true}).fill('no-match');await page.getByText('No loaded items match these filters.',{exact:false}).waitFor();
  await page.getByRole('button',{name:'Clear filters',exact:true}).click();
  await page.getByLabel('Content type',{exact:true}).fill('json');assert.equal(await page.locator('li').count(),1);
  await page.getByRole('button',{name:'reading.json · sensor',exact:true}).click();
  await page.getByRole('button',{name:'Decrypt and verify chunk',exact:true}).click();
  await page.getByTestId('data-preview').waitFor();assert.match(await page.getByTestId('data-preview').textContent(),/temperature/);
  assert.equal(await page.locator('pre script').count(),0,'preview escapes HTML');
  const downloadPromise=page.waitForEvent('download');await page.getByRole('button',{name:'Download verified chunk',exact:true}).click();const download=await downloadPromise;assert.match(download.suggestedFilename(),/reading.json/);assert.equal(JSON.parse(await fs.readFile(await download.path(),'utf8')).temperature,23);
  await page.getByLabel('Reader address',{exact:true}).fill('0x123');await page.getByRole('button',{name:'Grant read access',exact:true}).click();
  await page.getByText('Read grant confirmed.',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>window.fixtureState.grants[0].stream),'sensor');
  await page.getByLabel('All streams on this device',{exact:true}).check();await page.getByRole('button',{name:'Revoke read access',exact:true}).click();
  await page.getByText('Read grant revoked.',{exact:false}).waitFor();
  assert.equal(await page.evaluate(()=>window.fixtureState.grants[1].stream),undefined);
  assert.equal(await page.evaluate(()=>window.fixtureState.grants[1].allowed),false);
  await page.getByLabel('Upload URL',{exact:true}).fill('https://customer.invalid/{name}');await page.getByLabel('Optional bearer token',{exact:true}).fill('fixture-token');
  await page.getByRole('button',{name:'Export filtered chunks',exact:true}).click();await page.getByText('Exported 1 verified chunks.',{exact:false}).waitFor();
  assert.match(uploads[0].body,/temperature/);assert.equal(uploads[0].headers.authorization,'Bearer fixture-token');assert.equal(uploads[0].headers.cookie,undefined);
  await page.getByRole('button',{name:'Clear filters',exact:true}).click();
  await page.getByRole('button',{name:'Export filtered chunks',exact:true}).click();await page.getByText('Exported 4 verified chunks.',{exact:false}).waitFor();assert.equal(uploads.length,5);assert.equal(new Set(uploads.slice(1).map(x=>x.url)).size,4);
  refuseUpload=true;await page.getByRole('button',{name:'Export filtered chunks',exact:true}).click();await page.getByText('Export refused (403); check destination CORS and upload permission',{exact:true}).waitFor();refuseUpload=false;
  await page.getByLabel('Corrupt ciphertext',{exact:true}).check();await page.getByRole('button',{name:'Decrypt and verify chunk',exact:true}).click();await page.getByText('Opaque ciphertext digest mismatch',{exact:true}).waitFor();assert.equal(await page.getByTestId('data-preview').count(),0);
  await page.getByLabel('Corrupt ciphertext',{exact:true}).uncheck();await page.getByLabel('Deny keys',{exact:true}).check();await page.getByRole('button',{name:'Decrypt and verify chunk',exact:true}).click();await page.getByText('Access denied: scoped read grant unavailable',{exact:true}).waitFor();
  await page.getByLabel('Deny keys',{exact:true}).uncheck();await page.getByRole('button',{name:'Clear filters',exact:true}).click();
  await page.getByRole('button',{name:'multipart.txt · bulk',exact:true}).first().click();await page.getByRole('button',{name:'Verify complete upload',exact:true}).click();await page.getByRole('button',{name:'Download complete upload',exact:true}).waitFor();
  assert.equal((await page.getByTestId('data-preview').textContent()).length,24579);
  await page.getByRole('button',{name:'Change account/network or expire session',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('input[type=password]')?.value==='');assert.equal(await page.getByLabel('Upload URL',{exact:true}).inputValue(),'');assert.equal(await page.getByTestId('data-preview').count(),0);
  assert.ok(await page.evaluate(()=>window.fixtureState.buffers.every(b=>b.every(x=>x===0))),'all retained verified buffers wiped');
  await page.getByRole('button',{name:'Browse fleet data',exact:true}).click();await page.getByRole('button',{name:'capture.bin · camera',exact:true}).click();await page.getByRole('button',{name:'Decrypt and verify chunk',exact:true}).click();await page.getByText('Preview unavailable (binary, invalid UTF-8/JSON, or over 32 KiB). Download verified bytes instead.',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Reader role',exact:true}).click();assert.equal(await page.getByRole('button',{name:'Grant read access',exact:true}).count(),0);
  await page.getByRole('button',{name:'Change account/network or expire session',exact:true}).click();await page.getByRole('button',{name:'Browse fleet data',exact:true}).click();
  await page.getByRole('button',{name:'reading.json · sensor',exact:true}).click();await page.getByLabel('Delay open',{exact:true}).check();await page.getByRole('button',{name:'Decrypt and verify chunk',exact:true}).click();await page.waitForFunction(()=>window.fixtureState.pending>0);await page.getByRole('button',{name:'Change account/network or expire session',exact:true}).click();
  // Wait for the actual delayed fixture to settle by observing its retained buffer.
  await page.waitForFunction(()=>window.fixtureState.pending===0&&window.fixtureState.buffers.every(b=>b.every(x=>x===0)));
  assert.equal(await page.getByTestId('data-preview').count(),0);
  assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);assert.deepEqual(errors,[]);
  console.log('PASS real Chromium Data UI: filters/details, scoped decrypt/tamper/denial, safe preview/download, grant/revoke PTBs, export, assembly, scope changes and memory-only credentials');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
