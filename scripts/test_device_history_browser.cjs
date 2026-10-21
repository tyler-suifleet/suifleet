const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),http=require('node:http'),assert=require('node:assert/strict');
const {appRequire}=require('./frontend_test_utils.cjs');
const {build}=appRequire('esbuild'),{chromium}=appRequire('playwright');
(async()=>{
 const directory=await fs.mkdtemp(path.join(os.tmpdir(),'suifleet-history-browser-'));let browser,server;
 try{
  const entry=path.join(__dirname,'browser/device-history-acceptance.jsx');const manifest=await fs.readFile(path.join(__dirname,'../manifests/fixture.localnet.json'),'utf8');
  await build({entryPoints:[entry],outfile:path.join(directory,'app.js'),bundle:true,platform:'browser',jsx:'automatic',alias:{'@':path.resolve(__dirname,'../dapp/src')},nodePaths:[path.resolve(__dirname,'../dapp/node_modules')],plugins:[{name:'history-fixture-hooks',setup(b){b.onResolve({filter:/^(@mysten\/dapp-kit-react|@\/hooks\/(useDevices|useGroups|useDeployments|useDesiredStates|useFleetHealth|useFleetTransaction|useDeploymentStorage))$/},()=>({path:entry}));}}],define:{'process.env':'{}','process.env.NODE_ENV':'"development"','process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST':JSON.stringify(manifest)},logLevel:'warning'});
  server=http.createServer(async(request,response)=>{response.setHeader('Content-Type',request.url==='/app.js'?'text/javascript':'text/html');response.end(request.url==='/app.js'?await fs.readFile(path.join(directory,'app.js')):'<!doctype html><div id="root"></div><script src="/app.js"></script>');});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const origin=`http://127.0.0.1:${server.address().port}`;
  browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||undefined,headless:true,args:['--no-sandbox']});const page=await browser.newPage();page.setDefaultTimeout(10000);const errors=[];page.on('pageerror',error=>{errors.push(error.message);console.error(error.message)});
  await page.goto(origin+'/deployments');const history=page.getByRole('region',{name:'Device update history'});
  await history.getByRole('link',{name:'Alpha Pi',exact:true}).first().waitFor();assert.equal(await history.getByText('Failed',{exact:true}).count(),1);assert.equal(await history.getByText('Applied',{exact:true}).count(),2);
  await history.getByRole('searchbox',{name:'Search device name'}).fill('beta');assert.equal(await history.getByRole('link',{name:'Alpha Pi',exact:true}).count(),0);assert.equal(await history.getByRole('link',{name:'Beta Pi',exact:true}).count(),1);
  await history.getByRole('searchbox',{name:'Search device name'}).fill('');await history.getByRole('searchbox',{name:'Search current device group'}).fill('production');assert.equal(await history.getByRole('link',{name:'Beta Pi',exact:true}).count(),1);assert.equal(await history.getByText('Failed',{exact:true}).count(),0);
  await history.getByRole('searchbox',{name:'Search current device group'}).fill('missing');await history.getByText(/No device updates match/).waitFor();
  await history.getByRole('searchbox',{name:'Search current device group'}).fill('');await history.getByRole('combobox',{name:'Sort',exact:true}).selectOption('name-desc');assert.equal(await history.locator('tbody tr').first().getByRole('link').first().innerText(),'Beta Pi');
  await page.goto(origin+'/devices?device=0xda');await page.getByRole('heading',{name:'Alpha Pi — device details'}).waitFor();await page.getByText(/Last reported: test-app 1.0.2 \(installed\) \(one-shot\)/).first().waitFor();
  const detail=page.locator('[data-testid=health-details]');assert.match(await detail.innerText(),/Current groups: Lab/);assert.equal(await detail.getByText('Failed',{exact:true}).count(),1);assert.equal(await detail.getByText('Applied',{exact:true}).count(),1);
  await page.goto(origin+'/deployments?device=0xdb');await history.getByRole('link',{name:'Beta Pi',exact:true}).waitFor();assert.equal(await history.getByRole('link',{name:'Alpha Pi',exact:true}).count(),0);
  await page.goto(origin+'/deployments?group=0xfeed');await history.getByRole('link',{name:'Beta Pi',exact:true}).waitFor();assert.equal(await history.getByRole('link',{name:'Alpha Pi',exact:true}).count(),0);
  await page.goto(origin+'/devices?device=0xdead');await page.getByRole('alert').filter({hasText:/device is unavailable/}).waitFor();
  assert.deepEqual(errors,[]);console.log('PASS actual pages: named device history, success/failure, search by device/group, sorting, version summary, stable device/group links, detailed history and unavailable device');
 }finally{if(browser)await browser.close();if(server)await new Promise(resolve=>server.close(resolve));await fs.rm(directory,{recursive:true,force:true});}
})().catch(error=>{console.error(error);process.exitCode=1});
