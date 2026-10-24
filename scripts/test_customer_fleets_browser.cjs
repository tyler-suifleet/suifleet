// Actual provider, switcher, scoped query and page reset in Chromium. Wallet
// and transport are deterministic fixtures; chain authority is tested separately.
const fs=require("node:fs/promises"), path=require("node:path"), assert=require("node:assert/strict");
const {appRequire}=require("./frontend_test_utils.cjs");
const {build}=appRequire("esbuild"),{chromium}=appRequire("playwright");
const root=path.resolve(__dirname,"..");
(async()=>{
 const m=JSON.parse(await fs.readFile("manifests/fixture.localnet.json","utf8"));const catalog=JSON.parse(await fs.readFile("interfaces/fleet-v1.json","utf8"));
 for(const type of catalog.optional_type_origins.customer)m.type_origins[type]=m.call_package_id;
 const aliases={"@mysten/dapp-kit-react":"scripts/fixtures/customer-wallet-browser.tsx","@/lib/customer-fleets":"scripts/fixtures/customer-discovery-browser.ts","@/hooks/useFleetTransaction":"scripts/fixtures/customer-transaction-browser.ts"};
 const result=await build({entryPoints:["scripts/fixtures/customer-fleets-browser.tsx"],bundle:true,write:false,platform:"browser",jsx:"automatic",define:{"process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST":JSON.stringify(JSON.stringify(m)),"process.env.NODE_ENV":JSON.stringify("production"),"process.env":"{}"},plugins:[{name:"fixture",setup(b){for(const [name,file]of Object.entries(aliases))b.onResolve({filter:new RegExp("^"+name.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")+"$")},()=>({path:path.join(root,file)}));b.onResolve({filter:/^@\//},args=>({path:path.join(root,"dapp/src",args.path.slice(2))+(args.path.endsWith("useFleetSelection")?".tsx":".ts")}));}}],nodePaths:[path.join(root,"dapp/node_modules")]});
 const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||undefined,headless:true,args:["--no-sandbox"]});
 try{
  const context=await browser.newContext({viewport:{width:1050,height:600}});await context.route("http://fleet.test/**",route=>route.fulfill({contentType:"text/html",body:"<style>body{font:16px system-ui;margin:40px;background:#f3f6fa}section{background:white;padding:20px;border:1px solid #bbc;border-radius:8px}button,select,input{font:inherit;margin:8px;padding:6px}h1{color:#173954}</style><div id=root></div>"}));
  async function open(){const page=await context.newPage();page.on("pageerror",e=>console.error("Browser:",e.message));await page.goto("http://fleet.test/");await page.addScriptTag({content:result.outputFiles[0].text});return page;}
  const page=await open(),errors=[];page.on("pageerror",e=>errors.push(e.message));
  const wait=async(text,p=page)=>p.waitForFunction(t=>document.body.textContent.includes(t),text).catch(async e=>{throw Error(`${text}: ${await p.textContent("body")}; ${e.message}`)});
  await wait("Alpha device");assert.ok(!(await page.textContent("body")).includes("Beta device"));
  await page.getByLabel("Draft note").fill("Production only");
  const second=await open();await wait("Alpha device",second);
  const G="0x"+"2".repeat(64),F="0x"+"f".repeat(64),A="0x"+"a".repeat(64),B="0x"+"b".repeat(64);
  await page.getByLabel("Fleet",{exact:true}).selectOption(G);await wait("Beta device");await wait("Beta device",second);
  assert.ok(!(await page.textContent("body")).includes("Alpha device"));assert.equal(await page.getByLabel("Draft note").inputValue(),"");
  await page.screenshot({path:"docs/validation/sf136-fleet-switcher.png"});
  await page.evaluate(B=>window.fleetTest.wallet(B),B);await wait("Separate customer device");assert.ok(!(await page.textContent("body")).includes("Beta device"));
  await page.evaluate(B=>window.fleetTest.wallet(B,"testnet"),B);await wait("Wallet network does not match");assert.ok(!(await page.textContent("body")).includes("Separate customer device"));
  await page.evaluate(A=>window.fleetTest.wallet(A),A);await wait("Beta device");
  await page.evaluate(()=>window.fleetTest.revoke());await page.getByRole("button",{name:"Refresh fleets"}).click();await wait("Select or create a fleet");assert.ok(!(await page.textContent("body")).includes("Beta device"));
  await page.evaluate(()=>window.fleetTest.revoke(false));await page.getByRole("button",{name:"Refresh fleets"}).click();await wait("Beta device");
  await page.getByRole("button",{name:"Create fleet",exact:true}).click();await page.getByLabel("Fleet name").fill("Experiments");await page.getByRole("button",{name:"Create",exact:true}).click();await page.waitForFunction(()=>[...document.querySelector("select").options].some(o=>o.textContent.includes("Experiments")));
  const commands=await page.evaluate(()=>window.fleetTest.state.transactions[0].commands);assert.equal(commands[0].MoveCall.function,"create_fleet");
  await page.evaluate(()=>window.fleetTest.fail());await page.getByRole("button",{name:"Refresh fleets"}).click();await wait("Discovery unavailable");assert.ok(!(await page.textContent("body")).includes("Beta device"));
  await page.evaluate(()=>{window.fleetTest.fail(false);window.fleetTest.delay();});await page.getByRole("button",{name:"Retry",exact:true}).click();
  await page.evaluate(B=>window.fleetTest.wallet(B),B);await page.evaluate(()=>window.fleetTest.release());await wait("Separate customer device");assert.ok(!(await page.textContent("body")).includes("Beta device"));
  assert.deepEqual(errors,[]);console.log("PASS: real Chromium switcher, scoped devices, concurrent tabs, form reset, account/network changes, revocation, errors, delayed reads and create-fleet UI");
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
