// Devices screen: a confirmed registration the inventory does not list yet blocks a duplicate.
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),http=require('node:http'),assert=require('node:assert/strict');
const {appRequire}=require('./frontend_test_utils.cjs');
const {build}=appRequire('esbuild'),{chromium}=appRequire('playwright');
(async()=>{
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'suifleet-devices-pending-browser-'));let browser,server;
 try{
  const entry=path.join(__dirname,'browser/devices-pending-acceptance.jsx');const manifest=await fs.readFile(path.join(__dirname,'../manifests/fixture.localnet.json'),'utf8');
  await build({entryPoints:[entry],outfile:path.join(directory,'app.js'),bundle:true,platform:'browser',jsx:'automatic',alias:{'@':path.resolve(__dirname,'../dapp/src')},nodePaths:[path.resolve(__dirname,'../dapp/node_modules')],plugins:[{name:'devices-fixture-hooks',setup(b){b.onResolve({filter:/^(@mysten\/dapp-kit-react|@\/hooks\/(useDevices|useGroups|useDeployments|useDesiredStates|useFleetHealth|useFleetTransaction|useDeploymentStorage))$/},()=>({path:entry}));}}],define:{'process.env':'{}','process.env.NODE_ENV':'"development"','process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST':JSON.stringify(manifest)},logLevel:'warning'});
  server=http.createServer(async(request,response)=>{response.setHeader('Content-Type',request.url==='/app.js'?'text/javascript':'text/html');response.end(request.url==='/app.js'?await fs.readFile(path.join(directory,'app.js')):'<!doctype html><div id="root"></div><script src="/app.js"></script>');});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const origin=`http://127.0.0.1:${server.address().port}`;
  browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||undefined,headless:true,args:['--no-sandbox']});const page=await browser.newPage();page.setDefaultTimeout(10000);const errors=[];page.on('pageerror',error=>{errors.push(error.message);console.error(error.message)});
  const dialogs=[];page.on('dialog',dialog=>{dialogs.push(dialog.message());void dialog.dismiss();});
  await page.goto(origin+'/devices');const open=page.getByRole('button',{name:'Register by address'});
  await page.getByRole('link',{name:'Alpha Pi',exact:true}).waitFor();assert.equal(await open.isDisabled(),false);
  const register=async(name,address)=>{await open.click();await page.locator('input[name=name]').fill(name);await page.locator('input[name=deviceAddress]').fill(address);await page.getByRole('button',{name:'Register',exact:true}).click();};
  const address='0x'+'2'.repeat(64);await register('Second Pi',address);
  // Confirmed but not indexed: the pending row shows, and registering again is disabled.
  await page.getByRole('status').filter({hasText:'Registered · adding to the list…'}).waitFor();
  assert.equal(await page.locator('form').count(),0,'modal closes after a confirmed registration');
  assert.equal(await open.isDisabled(),true,'pending registration blocks reopening Register by address');
  assert.equal(await page.evaluate(()=>window.acceptance.signCalls()),1);
  // Forcing the click while pending must not open the form or sign again.
  await open.click({force:true});assert.equal(await page.locator('form').count(),0);assert.equal(await page.evaluate(()=>window.acceptance.signCalls()),1);
  await page.evaluate(()=>window.acceptance.index());
  await page.getByRole('link',{name:'Second Pi',exact:true}).waitFor();
  await page.getByRole('status').filter({hasText:'adding to the list'}).waitFor({state:'detached'});
  assert.equal(await open.isDisabled(),false,'listing the device re-enables registration');
  assert.equal(await page.evaluate(()=>window.acceptance.signCalls()),1);assert.deepEqual(dialogs,[]);
  assert.deepEqual(errors,[]);console.log('PASS Devices screen: lagged registration shows pending, blocks a duplicate and re-enables once listed');
 }finally{if(browser)await browser.close();if(server)await new Promise(resolve=>server.close(resolve));await fs.rm(directory,{recursive:true,force:true});}
})().catch(error=>{console.error(error);process.exitCode=1});
