const fs=require('node:fs'),assert=require('node:assert/strict');
// Read-only Testnet repro: node scripts/verify_device_health.cjs MANIFEST DATA_CONFIG OWNER
// Uses public locators only; never loads a wallet or submits a transaction.
const {loader,appRequire}=require('./frontend_test_utils.cjs');
const [manifestPath, configPath, owner] = process.argv.slice(2);
assert.ok(manifestPath && configPath && owner, 'Expected MANIFEST DATA_CONFIG OWNER');
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=fs.readFileSync(manifestPath,'utf8');
const load=loader(),data=load('lib/fleet-data.ts'),sui=load('lib/sui.ts'),dm=load('lib/deployment-manifest.ts');
const config=data.parseDataConfig(fs.readFileSync(configPath,'utf8'));
const {SuiGrpcClient}=appRequire('@mysten/sui/grpc'),m=dm.requireManifest();
const React=appRequire('react'),{renderToStaticMarkup}=appRequire('react-dom/server');
const {HealthDetails,TelemetrySettings}=load('components/DeviceHealth.tsx');
(async()=>{
 assert.equal(m.chain_identifier,'4c78adac');
 const client=new SuiGrpcClient({network:'testnet',baseUrl:m.rpc_url});
 const devices=await sui.fetchDevices(client,owner);
 const active=devices.filter(d=>d.ownerCapId);assert.ok(active.length,'no activated testnet device');
 const health=await data.fetchChainHealth(client,config,active.map(d=>d.objectId));
 const device=active.find(d=>health[d.objectId].codes.includes('not-in-data-fleet'));assert.ok(device,JSON.stringify(health));
 const h=health[device.objectId];
 const html=renderToStaticMarkup(React.createElement(React.Fragment,null,React.createElement(TelemetrySettings,{configured:true}),React.createElement(HealthDetails,{health:h})));
 assert.ok(html.includes('Health comes from verified device reports.'));assert.ok(html.includes('This device is not in the configured data fleet.'));assert.doesNotMatch(html,/NEXT_PUBLIC_|not set up/);
 const {chromium}=appRequire('playwright'),browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||undefined,headless:true,args:['--no-sandbox']});
 try {const page=await browser.newPage();await page.setContent(html);assert.ok(await page.getByText('This device is not in the configured data fleet.',{exact:true}).isVisible());}finally{await browser.close();}
 console.log(JSON.stringify({chain:m.chain_identifier,package:m.call_package_id,fleet:config.fleetId,device:device.objectId,name:device.name,health:h,checks:['verified deployment and registry','actual activated device','verified FleetData bindings','absent DeviceData index','rendered header and detail in Chrome'],writes:0},null,2));
})().catch(e=>{console.error(e);process.exitCode=1});
