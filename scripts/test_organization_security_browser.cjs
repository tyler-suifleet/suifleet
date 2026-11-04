// Read-only UI acceptance; existing security/delay/multisig suites verify the real transactions.
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),http=require('node:http'),assert=require('node:assert/strict');
const {appRequire}=require('./frontend_test_utils.cjs'),{build}=appRequire('esbuild'),{chromium}=appRequire('playwright');
(async()=>{
 const root=path.resolve(__dirname,'..'),src=path.join(root,'dapp/src'),entry=path.join(__dirname,'browser/organization-security-fixture.jsx');
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'suifleet-security-ui-'));let server,browser;
 try {
 const manifest=JSON.parse(await fs.readFile(path.join(root,'manifests/fixture.localnet.json')));
 const catalog=JSON.parse(await fs.readFile(path.join(root,'interfaces/fleet-v1.json')));
 for(const group of ['customer','security_policy','security_delay','passkey_factor']) for(const name of catalog.optional_type_origins[group]) manifest.type_origins[name]=manifest.call_package_id;
 const replaced=new Set(['@mysten/dapp-kit-react','@/hooks/useCurrentAccount','@/hooks/useFleetSelection','@/hooks/useFleetTransaction','@/lib/security-policy','@/lib/security-delay','@/lib/second-person','@/lib/passkey-factor']);
 await build({entryPoints:[entry],outfile:path.join(dir,'app.js'),bundle:true,platform:'browser',jsx:'automatic',nodePaths:[path.join(root,'dapp/node_modules')],alias:{'@':src},define:{'process.env':'{}','process.env.NODE_ENV':'"production"','process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST':JSON.stringify(JSON.stringify(manifest))},plugins:[{name:'security-ui-fixture',setup(b){b.onResolve({filter:/.*/},a=>a.importer!==entry&&replaced.has(a.path)?{path:entry}:undefined)}}],logLevel:'warning'});
 let styles=await fs.readFile(path.join(src,'components/OrganizationSecurity.css'),'utf8');
 if(process.env.SECURITY_SCREENSHOTS){for(const f of await fs.readdir(path.join(root,'dapp/out/_next/static/chunks')))if(f.endsWith('.css')) styles+=await fs.readFile(path.join(root,'dapp/out/_next/static/chunks',f),'utf8');await fs.mkdir(process.env.SECURITY_SCREENSHOTS,{recursive:true});}
 server=http.createServer(async(req,res)=>{const js=req.url==='/app.js';res.setHeader('Content-Type',js?'text/javascript':'text/html');res.end(js?await fs.readFile(path.join(dir,'app.js')):`<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><style>${styles} body{margin:0;background:#f8fafc;font-family:Arial,sans-serif}</style><div id="root"></div><script src="/app.js"></script>`)});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const origin=`http://127.0.0.1:${server.address().port}`;
 browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||undefined,headless:true,args:['--no-sandbox']});
 for(const [scenario,heading] of [['passkey','2 of 7 actions have extra protection'],['pending','2 of 7 actions have extra protection'],['configured','2 of 7 actions have extra protection'],['off','No additional protections enabled'],['no-delays','No additional protections enabled'],['legacy','Security setup needs attention'],['error','Protection status could not be verified'],['unsupported','Security settings unavailable'],['loading','Checking organization security…'],['unselected','Organization security']]){
  const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(`${origin}/?scenario=${scenario}`,{waitUntil:'networkidle'});await page.getByRole('heading',{name:heading,exact:true}).waitFor({timeout:10000}).catch(async()=>{throw Error(JSON.stringify({scenario,errors,body:await page.textContent('body')}))});
  assert.equal(await page.getByLabel('Ordered public keys, weights and threshold (JSON)').isVisible(),false,'advanced signing hidden by default');
  assert.equal(await page.getByText(/SF-\d+/).count(),0,'no tracker references in product guidance');
  if(scenario==='passkey'){
   const card=page.getByRole('article').filter({has:page.getByRole('heading',{name:'Passkey verification',exact:true})});
   await card.getByRole('button',{name:'View setup'}).click();
   assert.equal(await page.getByRole('button',{name:'Create passkey',exact:true}).isVisible(),true);
   assert.equal(await page.getByRole('button',{name:'Save or request passkey protection change for all actions',exact:true}).isVisible(),true);
  }
  if(scenario==='unsupported'){await page.locator('#security-passkeys > summary').click();assert.equal(await page.getByRole('button',{name:'Create passkey',exact:true}).count(),0);}
  if(scenario==='pending'){
   for(const width of [1280,390]){
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.locator('#security-delays').getAttribute('open'),null);
    assert.equal(await page.locator('#security-second-person').getAttribute('open'),null);
    for(const name of ['Change security requirements','set_security'])assert.equal(await page.getByRole('heading',{name,exact:true}).isVisible(),true,'pending change visible without expansion');
    for(const name of ['Cancel action','Execute action','Approve exact action','Reject','Cancel request'])assert.equal(await page.getByRole('button',{name,exact:true}).isVisible(),true,'review control visible without expansion');
    assert.equal(await page.getByRole('button',{name:'Execute action',exact:true}).isEnabled(),true);
    if(process.env.SECURITY_SCREENSHOTS){assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);await page.screenshot({path:path.join(process.env.SECURITY_SCREENSHOTS,`security-pending-${width}.png`),fullPage:true});}
   }
  }
  if(scenario==='configured'){
   assert.equal(await page.getByText('Configured · 2 actions',{exact:true}).count(),1);
   const delayCard=page.getByRole('article').filter({has:page.getByRole('heading',{name:'Delay and cancellation',exact:true})});
   await delayCard.getByRole('button',{name:'View setup'}).click();await page.getByText('Review windows',{exact:true}).waitFor();
   assert.equal(await page.locator('#security-delays').getAttribute('open'),'');await page.getByRole('button',{name:'Queue delay changes',exact:true}).waitFor();
   await page.locator('#security-delays > summary').click();
   await page.getByText('Protection by action',{exact:false}).first().click();await page.getByText('Multiple signers + Delay and cancellation',{exact:true}).waitFor();
   await page.getByText('Protection by action',{exact:false}).first().click();
   if(process.env.SECURITY_SCREENSHOTS)for(const width of [1280,390]){await page.setViewportSize({width,height:1000});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`overflow at${width}`);await page.screenshot({path:path.join(process.env.SECURITY_SCREENSHOTS,`security-${width}.png`),fullPage:true});}
  }
  if(scenario==='no-delays'){await page.locator('#security-delays > summary').click();assert.equal(await page.locator('#security-delays input[type=checkbox]:disabled').count(),7,'delay cannot be enabled before configuration');await page.getByRole('button',{name:'Save delays',exact:true}).waitFor();}
  const summary=page.getByText('Advanced multi-signature tools',{exact:false}).first();await summary.focus();await page.keyboard.press('Enter');
  const config=page.getByLabel('Ordered public keys, weights and threshold (JSON)');await config.waitFor();await config.fill('{"keys":[],"weights":[],"threshold":3}');
  await summary.click();await summary.click();assert.match(await config.inputValue(),/"threshold":3/,'disclosure retains edits');
  if(['legacy','error','unsupported','loading'].includes(scenario))assert.equal(await page.getByRole('heading',{name:'No additional protections enabled',exact:true}).count(),0,'unverified policy must not show off');
  assert.deepEqual(errors,[]);await page.close();
 }
 console.log('PASS: security overview pending/configured/off/legacy/error/unsupported/loading/unselected states, honest factor status, no ticket labels, setup navigation, keyboard disclosure and retained edits; optional desktop/mobile screenshots');
 } finally {if(browser)await browser.close();if(server)await new Promise(r=>server.close(r));await fs.rm(dir,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1});
