// Read-only: verify the sealed public assets through the real browser transport.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {appRequire}=require('../scripts/frontend_test_utils.cjs'),{chromium}=appRequire('playwright');
(async()=>{
 const release=path.resolve(process.argv[2]),origins=JSON.parse(fs.readFileSync(process.argv[3]||'hosting/production-origins.json'));
 const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE,headless:true,args:['--no-sandbox']});
 const evidence=[];
 try{
  for(const [role,key,entry] of [['marketing','origin','/how-it-works/'],['console','app_url','/get-started/'],['docs','docs_url','/']]){
   const origin=new URL(origins[key]).origin,record=JSON.parse(fs.readFileSync(path.join(release,role,'release.json')));
   const page=await browser.newPage(),response=await page.goto(origin+entry,{waitUntil:'networkidle'});assert.equal(response.status(),200);
   const headers=await response.allHeaders();
   for(const [key,value] of [['content-security-policy',"frame-ancestors 'none'"],['strict-transport-security','max-age=31536000'],['x-content-type-options','nosniff'],['referrer-policy','strict-origin-when-cross-origin']])assert.ok(headers[key]?.includes(value),origin+' missing '+key);
   const entries=Object.entries(record.files).filter(([name])=>!['_headers','404.html','404/index.html'].includes(name));
   for(let i=0;i<entries.length;i+=6){
    const results=await page.evaluate(async files=>Promise.all(files.map(async([name,expected])=>{
     const pathname=name==='index.html'?'/':name.endsWith('/index.html')?'/'+name.slice(0,-10):'/'+name;
     const response=await fetch(pathname,{cache:'no-store',credentials:'omit'}),bytes=await response.arrayBuffer();
     const actual=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(v=>v.toString(16).padStart(2,'0')).join('');
     return {name,status:response.status,expected,actual};
    })),entries.slice(i,i+6));
    for(const item of results){assert.equal(item.status,200,origin+'/'+item.name);assert.equal(item.actual,item.expected,origin+'/'+item.name+' differs from sealed artifact');}
   }
   evidence.push({origin,revision:record.revision,files:entries.length,headers:Object.fromEntries(['content-security-policy','strict-transport-security','x-content-type-options','referrer-policy'].map(k=>[k,headers[k]]))});
   console.log('PASS exact sealed served bytes and actual response headers:',origin,entries.length,'files');await page.close();
  }
  const page=await browser.newPage(),response=await page.goto('https://www.suifleet.io/',{waitUntil:'networkidle'});assert.equal(response.status(),200);assert.match(await page.title(),/SuiFleet/);console.log('PASS www HTTPS website');
  if(process.env.SERVED_EVIDENCE)fs.writeFileSync(process.env.SERVED_EVIDENCE,JSON.stringify(evidence,null,2)+'\n');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
