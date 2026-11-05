const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs/promises');
const {appRequire}=require('./frontend_test_utils.cjs');
const {build}=appRequire('esbuild'),{chromium}=appRequire('playwright');
(async()=>{
 const fixture=path.resolve(__dirname,'browser/reporting-fixture.jsx');
 const aliases=Object.fromEntries(['@/hooks/useFleetSelection','@/hooks/useFleetDataConfig','@/hooks/useFleetTransaction','@/lib/fleet-reporting','@mysten/dapp-kit-react'].map(name=>[name,fixture]));
 const {outputFiles}=await build({entryPoints:[fixture],bundle:true,platform:'browser',write:false,jsx:'automatic',nodePaths:[path.resolve(__dirname,'../dapp/node_modules')],alias:aliases,define:{'process.env.NODE_ENV':'"development"'}});
 const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
 try{
  const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('https://reporting.invalid/**',r=>r.fulfill({contentType:'text/html',body:'<div id="root"></div><script src="https://bundle.invalid/app.js"></script>'}));
  await page.route('https://bundle.invalid/app.js',r=>r.fulfill({contentType:'text/javascript',body:outputFiles[0].text}));
  await page.goto('https://reporting.invalid/');
  await page.getByText('Pending: checking reporting readiness…',{exact:true}).waitFor();
  assert(await page.getByRole('button',{name:'Set up fleet reporting',exact:true}).isDisabled());
  await page.getByRole('button',{name:'Refresh readiness',exact:true}).click();
  await page.getByRole('button',{name:'Simulate unavailable readiness'}).click();
  await page.getByText('Failed: readiness could not be verified.',{exact:true}).waitFor();
  assert(await page.getByRole('button',{name:'Set up fleet reporting',exact:true}).isDisabled());
  await page.getByRole('button',{name:'Refresh readiness',exact:true}).click();
  await page.getByRole('button',{name:'Set up fleet reporting',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'User rejected'}).waitFor();
  await page.getByRole('button',{name:'Set up fleet reporting',exact:true}).click();
  await page.getByText('Verified: fleet reporting and storage bindings.',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Download device reporting settings',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'Enroll device'}).waitFor();
  await page.getByRole('button',{name:'Enable device reporting',exact:true}).click();
  const pending=page.waitForEvent('download');await page.getByRole('button',{name:'Download device reporting settings',exact:true}).click();
  const download=await pending;assert.equal(download.suggestedFilename(),'fleet-reporting.toml');assert.match(await fs.readFile(await download.path(),'utf8'),/verified-data/);
  await page.getByText('Reporting remains unreported until the daemon submits a verified sample.',{exact:false}).waitFor();
  await page.getByRole('button',{name:'Select second fleet',exact:true}).click();
  assert.equal(await page.getByRole('button',{name:'Download device reporting settings',exact:true}).count(),0);
  await page.getByRole('button',{name:'Revoke administrator',exact:true}).click();
  assert(await page.getByRole('button',{name:'Set up fleet reporting',exact:true}).isDisabled());
  assert.deepEqual(errors,[]);
  console.log('PASS: browser readiness failure/refresh, rejected setup/retry, verified setup, enrollment/download guard, TOML download, fleet isolation and revoked administrator controls (fixture boundaries)');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
