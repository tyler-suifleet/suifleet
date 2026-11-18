// Actual public HTTPS video delivery; optional origins file, no wallet or writes.
const assert=require('node:assert/strict'),fs=require('node:fs');
const {appRequire}=require('../scripts/frontend_test_utils.cjs'),{chromium}=appRequire('playwright');
(async()=>{
 const origins=JSON.parse(fs.readFileSync(process.argv[2]||'hosting/production-origins.json'));
 const origin=new URL(origins.origin);assert.equal(origin.protocol,'https:');assert.equal(origin.origin,origins.origin);
 const manifest=JSON.parse(fs.readFileSync('site/media/overview/manifest.json'));
 const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE,headless:true,args:['--no-sandbox']});
 try{
  for(const width of [390,1440]){
   const page=await browser.newPage({viewport:{width,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
   const response=await page.goto(origin+'/how-it-works/',{waitUntil:'networkidle'});assert.equal(response.status(),200);
   const video=page.locator('#overview video:visible');assert.equal(await video.count(),1);await video.scrollIntoViewIfNeeded();
   assert.equal(await video.evaluate(v=>v.paused),true);assert.equal(await video.locator('track').evaluate(t=>t.default),false);
   assert.equal(await video.evaluate(v=>v.textTracks[0].mode),'disabled');
   const variant=manifest.variants.find(v=>v.name===(width===390?'portrait':'landscape'));
   assert.ok((await video.locator('source').first().getAttribute('src')).includes(variant.file_sha256.slice(0,16)));
   await video.evaluate(async v=>{v.muted=true;try{await v.play()}catch(e){if(e.name!=='AbortError'||!v.src.startsWith('blob:'))throw e}});
   await page.waitForFunction(()=>[...document.querySelectorAll('#overview video')].some(v=>getComputedStyle(v).display!=='none'&&v.currentTime>.2));
   await video.evaluate(v=>v.textTracks[0].mode='showing');
   await page.waitForFunction(()=>[...document.querySelectorAll('#overview video')].some(v=>getComputedStyle(v).display!=='none'&&v.textTracks[0].cues?.length===7));
   assert.deepEqual(await video.evaluate(v=>[v.videoWidth,v.videoHeight]),width===390?[720,1280]:[1280,720]);
   await page.waitForFunction(()=>[...document.querySelectorAll('#overview video')].some(v=>getComputedStyle(v).display!=='none'&&v.dataset.overviewPrepared==='true'));
   await video.evaluate(v=>{v.textTracks[0].mode='disabled';v.currentTime=40});
   await page.waitForFunction(()=>[...document.querySelectorAll('#overview video')].some(v=>getComputedStyle(v).display!=='none'&&v.currentTime>=40&&!v.seeking));
   assert.equal(await video.evaluate(v=>v.textTracks[0].mode),'disabled');await video.evaluate(v=>v.pause());
   await page.getByText('Read the complete narration',{exact:true}).click();assert.equal(await page.locator('.overview-transcript li:visible').count(),7);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.deepEqual(errors,[]);
   console.log('PASS owned HTTPS overview version/playback/captions/seek/transcript:',width);await page.close();
  }
  const page=await browser.newPage();await page.goto(origin+'/');const pending=page.waitForEvent('download');
  await page.evaluate(()=>{const a=document.createElement('a');a.href='/setup/suifleet-dns.zone';a.download='suifleet-dns.zone';document.body.appendChild(a);a.click()});
  const download=await pending;assert.equal(fs.readFileSync(await download.path(),'utf8'),fs.readFileSync('hosting/cloudflare-dns.zone','utf8'));
  console.log('PASS exact Cloudflare-only DNS download');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
