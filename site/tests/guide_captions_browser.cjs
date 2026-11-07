// Read-only acceptance of every published guide player. No wallet or chain calls.
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
const {appRequire}=require('../../scripts/frontend_test_utils.cjs'),{chromium}=appRequire('playwright');
(async()=>{
 const origin=process.argv[2];assert.ok(origin?.startsWith('https://'),'Supply the actual HTTPS origin');
 const manifest=JSON.parse(await fs.readFile(path.resolve(__dirname,'../media/guide/videos.json'),'utf8'));
 const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||'/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
 try{for(const width of [390,1440]){
  const page=await browser.newPage({viewport:{width,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  const response=await page.goto(origin+'/guide/',{waitUntil:'networkidle'});assert.equal(response.status(),200);
  assert.equal(await page.locator('video track[default]').count(),0);
  for(const v of await page.locator('.guide-stage video').all())assert.equal(await v.evaluate(v=>v.textTracks[0].mode),'disabled');
  for(const clip of manifest.videos){
   await page.getByLabel('Guide stage').selectOption(clip.step);const v=page.locator(`#${clip.step} video`);
   await v.evaluate(async v=>{v.muted=true;v.textTracks[0].mode='hidden';await v.play()});
   await page.waitForFunction(id=>{const v=document.querySelector(`#${id} video`);return v.currentTime>.1&&v.textTracks[0].cues?.length>0},clip.step);
   assert.deepEqual(await v.evaluate(v=>[v.videoWidth,v.videoHeight]),[clip.width,clip.height]);
   const source=new URL(await v.evaluate(v=>v.currentSrc));const key=source.pathname.endsWith('.mp4')?'fallback':'file';assert.equal(source.searchParams.get('v'),clip[key+'_sha256'].slice(0,16));
   const cues=await v.evaluate(v=>[...v.textTracks[0].cues].map(c=>c.text.replace(/\s+/g,' ').trim()));assert.deepEqual(cues,clip.narration.cues.map(c=>c.text));
   await v.evaluate(v=>{v.textTracks[0].mode='showing';v.currentTime=v.textTracks[0].cues[0].startTime+.1});
   await page.waitForFunction(id=>document.querySelector(`#${id} video`).textTracks[0].activeCues?.length===1,clip.step);
   await v.evaluate(v=>{v.textTracks[0].mode='disabled';v.pause()});assert.equal(await v.evaluate(v=>v.textTracks[0].mode),'disabled');
  }
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.deepEqual(errors,[]);
  console.log('PASS every guide: captions off by default/on/off, complete narration cues, corrected video dimensions and versioned live media:',width);await page.close();
 }}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
