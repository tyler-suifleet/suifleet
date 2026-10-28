const fs=require("node:fs/promises"),path=require("node:path"),assert=require("node:assert/strict");
const {appRequire}=require("./frontend_test_utils.cjs"),{build}=appRequire("esbuild"),{chromium}=appRequire("playwright");
(async()=>{
 const root=path.resolve(__dirname,".."),entry=path.join(root,"scripts/browser/entitlements-fixture.jsx");
 const m=JSON.parse(await fs.readFile("manifests/fixture.localnet.json"));
 for(const t of JSON.parse(await fs.readFile("interfaces/fleet-v1.json")).optional_type_origins.customer)m.type_origins[t]=m.call_package_id;
 const result=await build({entryPoints:[entry],bundle:true,write:false,platform:"browser",jsx:"automatic",nodePaths:[path.join(root,"dapp/node_modules")],alias:{"@":path.join(root,"dapp/src")},plugins:[{name:"fixtures",setup(b){b.onResolve({filter:/^(@mysten\/dapp-kit-react|@\/hooks\/(useFleetSelection|useFleetTransaction))$/},()=>({path:entry}));}}],define:{"process.env":"{}","process.env.NODE_ENV":JSON.stringify("production"),"process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST":JSON.stringify(JSON.stringify(m))}});
 const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||"/opt/google/chrome/chrome",headless:true,args:["--no-sandbox"]});
 try{
 const page=await browser.newPage(),errors=[];page.on("pageerror",e=>errors.push(e.message));
 await page.route("http://fleet.test/**",r=>r.fulfill({contentType:"text/html",body:"<div id=root></div>"}));
 async function open(admin){await page.goto("http://fleet.test/"+(admin?"?admin":""));await page.addScriptTag({content:result.outputFiles[0].text});}
 await open(false);await page.getByText("Your connected account cannot grant entitlements.",{exact:false}).waitFor();assert.equal(await page.getByRole("button",{name:"Grant device limit"}).count(),0);
 assert.match(await page.textContent("body"),/0 active devices \/ 0 allowed/);
 await open(true);await page.getByRole("button",{name:"Grant device limit"}).waitFor();
 await page.getByLabel("Customer ID",{exact:true}).fill(await page.evaluate(()=>window.entitlements.customer));
 await page.getByLabel("Device limit",{exact:true}).fill("-1");await page.getByRole("button",{name:"Grant device limit"}).click();await page.getByRole("status").waitFor();assert.equal(await page.evaluate(()=>window.entitlements.calls.length),0);
 await page.getByLabel("Device limit",{exact:true}).fill("3");await page.getByRole("button",{name:"Grant device limit"}).click();await page.getByText("Device limit saved for Test customer: 3.",{exact:true}).waitFor();
 assert.equal(await page.evaluate(()=>window.entitlements.calls[0].commands[0].MoveCall.function),"set_entitlement");assert.deepEqual(errors,[]);
 console.log("PASS: actual entitlement component in Chromium: zero-limit guidance, unauthorized refusal, invalid grant rejected before signing, registry-admin grant via real SDK builder; fixture transport/wallet");
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
