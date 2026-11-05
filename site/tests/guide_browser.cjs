// Full guide navigation against the built site, real CSP, recordings and responsive layout.
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),http=require('node:http'),assert=require('node:assert/strict'),{execFileSync}=require('node:child_process');
const {chromium}=require(process.env.SUIFLEET_PLAYWRIGHT_MODULE||'../../dapp/node_modules/playwright');
(async()=>{
 const repo=path.resolve(__dirname,'../..'),temporary=process.argv[2]?null:await fs.mkdtemp(path.join(os.tmpdir(),'suifleet-guide-'));
 const missingMedia=new Set();
 const root=path.resolve(process.argv[2]||path.join(temporary,'site')),shots=process.argv[3];let server,browser;
 try {
 if(temporary)execFileSync('python3',['site/build.py','--out',root],{cwd:repo,stdio:'pipe'});
 const policy=(await fs.readFile(path.join(root,'_headers'),'utf8')).split('Content-Security-Policy: ')[1].split('\n')[0];
 const steps=JSON.parse(await fs.readFile(path.join(repo,'site/guide/steps.json'))).steps;
 const expected=new Map(steps.flatMap(step=>[step.summary,...step.before,...step.actions,step.result].map((text,i)=>[`${step.id}/${i}`,text.replace(/\s+/g,' ').trim()])));
 server=http.createServer(async(req,res)=>{try{const pathname=new URL(req.url,'http://local').pathname;if(missingMedia.has(pathname)){res.statusCode=404;res.end();return;}let name=path.resolve(root,'.'+pathname);if(!name.startsWith(root+path.sep))throw Error('outside root');if(pathname.endsWith('/'))name=path.join(name,'index.html');const data=await fs.readFile(name);res.setHeader('Content-Type',({'.html':'text/html','.css':'text/css','.js':'application/javascript','.svg':'image/svg+xml','.jpg':'image/jpeg','.png':'image/png','.woff2':'font/woff2','.webm':'video/webm','.mp4':'video/mp4','.vtt':'text/vtt'})[path.extname(name)]||'application/octet-stream');res.setHeader('Content-Security-Policy',policy);res.setHeader('X-Content-Type-Options','nosniff');res.setHeader('Content-Length',data.length);res.end(data)}catch{res.statusCode=404;res.end()}});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const origin=`http://127.0.0.1:${server.address().port}`;
 browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||undefined,headless:true,args:['--no-sandbox']});if(shots)await fs.mkdir(shots,{recursive:true});
 for(const [width,height] of [[1440,900],[1280,720],[390,844],[375,667],[320,568],[844,390],[640,360]]){
  const page=await browser.newPage({viewport:{width,height}}),errors=[],external=[];page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')errors.push(m.text())});page.on('request',r=>{if(new URL(r.url()).origin!==origin)external.push(r.url())});
  await page.goto(origin+'/guide/',{waitUntil:'networkidle'});await page.waitForFunction(()=>!document.querySelector('[data-guide-next]').disabled);await page.evaluate(()=>document.fonts.ready);
  await page.locator('[data-guide-next]').click();await page.locator('#capacity-plan').waitFor();await page.locator('[data-guide-back]').click();await page.locator('#sign-in').waitFor();
  const gathered=new Map();let count=0;
  while(!(await page.locator('[data-guide-complete]').isVisible())){
   const value=await page.evaluate(()=>{const stage=[...document.querySelectorAll('.guide-stage')].find(s=>!s.hidden),copy=stage.querySelector('.guide-copy'),controls=document.querySelector('.guide-controls').getBoundingClientRect();return {key:`${stage.id}/${stage.dataset.instruction}`,text:copy.textContent.trim(),width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight,viewport:[innerWidth,innerHeight],fits:copy.scrollHeight<=copy.clientHeight&&copy.scrollWidth<=copy.clientWidth,copyHeight:copy.clientHeight,controls:[controls.top,controls.bottom],part:Number(stage.dataset.part)}});
   assert.equal(value.width<=width&&value.height<=height,true,`page overflow${width}x${height}: ${JSON.stringify(value)}`);assert.ok(value.fits&&value.copyHeight>0,`clipped text${width}x${height}: ${JSON.stringify(value)}`);assert.ok(value.controls[0]>=0&&value.controls[1]<=height,`controls outside viewport${width}x${height}`);
   if(value.part===0)gathered.set(value.key,[]);gathered.get(value.key).push(value.text);
   if(count===0&&shots)await page.screenshot({path:path.join(shots,`guide-${width}x${height}.png`)});
   await page.evaluate(()=>{const stage=[...document.querySelectorAll('.guide-stage')].find(s=>!s.hidden),forward=stage.querySelector('[data-guide-forward]');(forward.disabled?document.querySelector('[data-guide-next]'):forward).click()});assert.ok(++count<1500,'navigation did not finish');
  }
  assert.equal(gathered.size,expected.size,'every prerequisite, action and result remains reachable');for(const [key,text]of expected)assert.equal(gathered.get(key).join(' '),text,`lost/duplicated text${width}x${height} ${key}`);
  await page.locator('[data-guide-back]').click();assert.equal(await page.locator('[data-guide-complete]').isVisible(),false);await page.locator('[data-guide-next]').click();await page.locator('[data-guide-next]').click();assert.equal(await page.locator('[data-guide-back]').isDisabled(),true);
  await page.getByLabel('Guide stage').selectOption('publish-version');assert.equal(await page.locator('#publish-version').isVisible(),true);await page.goBack();await page.locator('#sign-in').waitFor();
  await page.goto(origin+'/guide/#fund',{waitUntil:'networkidle'});await page.locator('#fund').waitFor();assert.equal(await page.getByLabel('Guide stage').inputValue(),'fund');
  await page.mouse.wheel(0,800);assert.equal(await page.evaluate(()=>scrollY),0);
  assert.deepEqual(errors,[],`browser errors at${width}x${height}`);assert.deepEqual(external,[]);console.log(`PASS guide${width}x${height}: ${count} screens, all${expected.size} instructions, no page/text overflow, controls visible, back/next/jump/history/deep link`);await page.close();
  // Real server 404s exercise non-bubbling <source> errors, including a deep link.
  missingMedia.add('/media/guide/fund.webm');missingMedia.add('/media/guide/fund.mp4');
  const failure=await browser.newPage({viewport:{width,height}});
  await failure.goto(origin+'/guide/#fund',{waitUntil:'networkidle'});
  await failure.locator('#fund [data-video-error]').waitFor();
  assert.equal(await failure.locator('#fund video').isVisible(),false);
  assert.equal(await failure.locator('#fund video').evaluate(v=>v.networkState),3,'both source formats exhausted');
  assert.equal(await failure.locator('[data-guide-next]').isEnabled(),true,'video failure does not block instructions');
  assert.ok(await failure.locator('#fund [data-video-error]').evaluate(e=>{const a=e.getBoundingClientRect(),b=e.closest('figure').getBoundingClientRect();return a.top>=b.top&&a.bottom<=b.bottom}),`video error clipped at${width}x${height}`);
  console.log(`PASS real WebM/MP4 404 fallback at ${width}x${height}`);
  await failure.close();missingMedia.clear();
 }
 // Keyboard-only navigation must retain focus at disabled-control boundaries.
 const keyboard=await browser.newPage({viewport:{width:1280,height:720}});
 await keyboard.goto(origin+'/guide/',{waitUntil:'networkidle'});
 async function tabTo(selector) {
  for(let i=0;i<100;i++) {
   if(await keyboard.locator(selector).evaluate(e=>e===document.activeElement))return;
   await keyboard.keyboard.press('Tab');
  }
  assert.fail(`keyboard could not reach ${selector}`);
 }
 await tabTo('[data-guide-next]');await keyboard.keyboard.press('Enter');
 await tabTo('[data-guide-back]');await keyboard.keyboard.press('Enter');
 assert.equal(await keyboard.locator('#sign-in h2').evaluate(e=>e===document.activeElement),true,'Back to stage 1 focuses heading');
 let turns=0;
 while(!await keyboard.locator('#sign-in [data-guide-forward]').isDisabled()) {
  await tabTo('#sign-in [data-guide-forward]');await keyboard.keyboard.press('Enter');
  assert.ok(++turns<200);
 }
 assert.equal(await keyboard.locator('#sign-in h2').evaluate(e=>e===document.activeElement),true,'last instruction focuses heading');
 turns=0;
 while(!await keyboard.locator('#sign-in [data-guide-prev-part]').isDisabled()) {
  await tabTo('#sign-in [data-guide-prev-part]');await keyboard.keyboard.press('Enter');
  assert.ok(++turns<200);
 }
 assert.equal(await keyboard.locator('#sign-in h2').evaluate(e=>e===document.activeElement),true,'first instruction focuses heading');
 for(let i=0;i<steps.length;i++){await tabTo('[data-guide-next]');await keyboard.keyboard.press('Enter');}
 assert.equal(await keyboard.locator('[data-guide-complete]').evaluate(e=>e===document.activeElement),true,'Finish focuses completion');
 await tabTo('[data-guide-next]');await keyboard.keyboard.press('Enter');
 assert.equal(await keyboard.locator('#sign-in').isVisible(),true);
 console.log('PASS keyboard Tab/Enter: Back, both instruction boundaries, Finish and restart');await keyboard.close();
 // Exercise both JavaScript-disabled and failed-script delivery (noscript does not cover the latter).
 for(const javaScriptEnabled of [false,true]) {
  const fallback=await browser.newPage({javaScriptEnabled,viewport:{width:390,height:844}});
  if(javaScriptEnabled)await fallback.route('**/assets/guide.js*',route=>route.abort());
  await fallback.goto(origin+'/guide/',{waitUntil:'networkidle'});
  assert.equal(await fallback.locator('.guide-stage:visible').count(),steps.length);
  const items=await fallback.locator('[data-guide-instruction]').allTextContents();
  assert.deepEqual(items.map(t=>t.replace(/\s+/g,' ').trim()),[...expected.values()]);
  for(const item of await fallback.locator('[data-guide-instruction]').all()) {
   assert.equal(await item.isVisible(),true);
   assert.ok(await item.evaluate(e=>{const r=e.getBoundingClientRect();return r.height>0&&r.left>=0&&r.right<=innerWidth}),'fallback text fits phone width');
  }
  await fallback.locator('[data-guide-instruction]').last().scrollIntoViewIfNeeded();
  assert.ok(await fallback.locator('[data-guide-instruction]').last().evaluate(e=>e.getBoundingClientRect().bottom<=innerHeight),'last result reachable');
  console.log(`PASS static fallback: all ${items.length} instructions reachable, JavaScript ${javaScriptEnabled?'blocked':'disabled'}`);
  await fallback.close();
 }
 const page=await browser.newPage();await page.goto(origin+'/guide/',{waitUntil:'networkidle'});const video=page.locator('#sign-in video');
 await video.evaluate(async v=>{v.load();await v.play()});await page.waitForFunction(()=>document.querySelector('#sign-in video').currentTime>0);assert.equal(await video.evaluate(v=>v.error),null);
 await page.getByLabel('Guide stage').selectOption('fund');assert.equal(await video.evaluate(v=>v.paused),true,'leaving a stage pauses its video');

 missingMedia.add('/media/guide/fund.webm');
 await page.goto(origin+'/guide/#fund',{waitUntil:'networkidle'});
 await page.locator('#fund video').evaluate(async v=>{v.load();await v.play()});
 assert.ok(await page.locator('#fund video').evaluate(v=>v.currentSrc.endsWith('fund.mp4')),'MP4 plays after WebM 404');
 assert.equal(await page.locator('#fund [data-video-error]').isVisible(),false,'working MP4 is not marked unavailable');
 missingMedia.clear();
 // Native captions load from the same origin on desktop and mobile. Keyboard
 // focus stays on the native player; Space must start/pause real playback.
 for(const viewport of [{width:1280,height:720},{width:390,height:844}]) {
  const captionsPage=await browser.newPage({viewport});
  await captionsPage.goto(origin+'/guide/',{waitUntil:'networkidle'});
  const player=captionsPage.locator('#sign-in video');
  await player.evaluate(v=>{v.load();v.textTracks[0].mode='showing'});
  await captionsPage.waitForFunction(()=>{
   const v=document.querySelector('#sign-in video');return v.textTracks[0]?.cues?.length>0;
  });
  const manifest=JSON.parse(await fs.readFile(path.join(repo,'site/media/guide/videos.json'),'utf8'));
  for(const recording of manifest.videos) {
   const loaded=await captionsPage.locator(`#${recording.step} video`).evaluate(async v=>{
    const track=v.querySelector('track');
    if(track.readyState!==2)await new Promise((resolve,reject)=>{const timeout=setTimeout(()=>reject(Error('caption track did not load')),10000);track.addEventListener('load',()=>{clearTimeout(timeout);resolve()},{once:true});track.addEventListener('error',()=>{clearTimeout(timeout);reject(Error('caption load failed'))},{once:true});track.track.mode='hidden';v.preload='metadata';v.load()});
    return {language:track.srclang,kind:track.kind,cues:[...track.track.cues].map(c=>({start:c.startTime,end:c.endTime,text:c.text}))};
   });
   assert.equal(loaded.language,'en');assert.equal(loaded.kind,'captions');
   assert.equal(loaded.cues.length,recording.narration.cues.length);
   loaded.cues.forEach((cue,i)=>{
    const spoken=recording.narration.cues[i];
    assert.ok(Math.abs(cue.start-spoken.speech_start)<0.0011);
    assert.ok(Math.abs(cue.end-spoken.speech_end)<0.0011);
    assert.equal(cue.text.replace(/\s+/g,' ').trim(),spoken.text);
   });
  }
  await player.focus();await captionsPage.keyboard.press('Space');
  await captionsPage.waitForFunction(()=>!document.querySelector('#sign-in video').paused);
  await captionsPage.keyboard.press('Space');
  await captionsPage.waitForFunction(()=>document.querySelector('#sign-in video').paused);
  await player.evaluate(async v=>{v.textTracks[0].mode='showing';v.currentTime=v.textTracks[0].cues[0].startTime+0.1;await v.play()});
  await captionsPage.waitForFunction(()=>document.querySelector('#sign-in video').textTracks[0].activeCues.length===1);
  await player.evaluate(v=>v.pause());
  console.log(`PASS native English captions: every full narration/timing, keyboard Space playback, active caption at ${viewport.width}x${viewport.height}`);
  await captionsPage.close();
 }
 assert.equal(await page.locator('video source[type="video/mp4"]').count(),steps.length);console.log('PASS real recording playback, inactive video pause, MP4 fallbacks and video-error instructions');await page.close();
 } finally {if(browser)await browser.close();if(server)await new Promise(r=>server.close(r));if(temporary)await fs.rm(temporary,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1});
