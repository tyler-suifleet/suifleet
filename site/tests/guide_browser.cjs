// Full guide navigation against the built site, real CSP, recordings and responsive layout.
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),http=require('node:http'),assert=require('node:assert/strict'),{execFileSync}=require('node:child_process');
const {chromium}=require(process.env.SUIFLEET_PLAYWRIGHT_MODULE||'../../dapp/node_modules/playwright');
(async()=>{
 const repo=path.resolve(__dirname,'../..'),temporary=process.argv[2]?null:await fs.mkdtemp(path.join(os.tmpdir(),'suifleet-guide-'));
 const root=path.resolve(process.argv[2]||path.join(temporary,'site')),shots=process.argv[3];let server,browser;
 try {
 if(temporary)execFileSync('python3',['site/build.py','--out',root],{cwd:repo,stdio:'pipe'});
 const policy=(await fs.readFile(path.join(root,'_headers'),'utf8')).split('Content-Security-Policy: ')[1].split('\n')[0];
 const steps=JSON.parse(await fs.readFile(path.join(repo,'site/guide/steps.json'))).steps;
 const expected=new Map(steps.flatMap(step=>[step.summary,...step.before,...step.actions,step.result].map((text,i)=>[`${step.id}/${i}`,text.replace(/\s+/g,' ').trim()])));
 server=http.createServer(async(req,res)=>{try{const pathname=new URL(req.url,'http://local').pathname;let name=path.resolve(root,'.'+pathname);if(!name.startsWith(root+path.sep))throw Error('outside root');if(pathname.endsWith('/'))name=path.join(name,'index.html');const data=await fs.readFile(name);res.setHeader('Content-Type',({'.html':'text/html','.css':'text/css','.js':'application/javascript','.svg':'image/svg+xml','.jpg':'image/jpeg','.png':'image/png','.woff2':'font/woff2','.webm':'video/webm','.mp4':'video/mp4'})[path.extname(name)]||'application/octet-stream');res.setHeader('Content-Security-Policy',policy);res.setHeader('X-Content-Type-Options','nosniff');res.setHeader('Content-Length',data.length);res.end(data)}catch{res.statusCode=404;res.end()}});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const origin=`http://127.0.0.1:${server.address().port}`;
 browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||undefined,headless:true,args:['--no-sandbox']});if(shots)await fs.mkdir(shots,{recursive:true});
 for(const [width,height] of [[1440,900],[1280,720],[390,844],[375,667],[320,568],[844,390],[640,360]]){
  const page=await browser.newPage({viewport:{width,height}}),errors=[],external=[];page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')errors.push(m.text())});page.on('request',r=>{if(new URL(r.url()).origin!==origin)external.push(r.url())});
  await page.goto(origin+'/guide/',{waitUntil:'networkidle'});await page.waitForFunction(()=>!document.querySelector('[data-guide-next]').disabled);await page.evaluate(()=>document.fonts.ready);
  await page.locator('[data-guide-next]').click();await page.locator('#fund').waitFor();await page.locator('[data-guide-back]').click();await page.locator('#sign-in').waitFor();
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
  await page.locator('#fund video').evaluate(v=>v.dispatchEvent(new Event('error')));await page.locator('#fund [data-video-error]').waitFor();assert.equal(await page.locator('#fund video').isVisible(),false);
  assert.ok(await page.locator('#fund [data-video-error]').evaluate(e=>{const a=e.getBoundingClientRect(),b=e.closest('figure').getBoundingClientRect();return a.top>=b.top&&a.bottom<=b.bottom}),`video error clipped at${width}x${height}`);
  assert.deepEqual(errors,[],`browser errors at${width}x${height}`);assert.deepEqual(external,[]);console.log(`PASS guide${width}x${height}: ${count} screens, all${expected.size} instructions, no page/text overflow, controls visible, back/next/jump/history/deep link`);await page.close();
 }
 const page=await browser.newPage();await page.goto(origin+'/guide/',{waitUntil:'networkidle'});const video=page.locator('#sign-in video');
 await video.evaluate(async v=>{v.load();await v.play()});await page.waitForFunction(()=>document.querySelector('#sign-in video').currentTime>0);assert.equal(await video.evaluate(v=>v.error),null);
 await page.getByLabel('Guide stage').selectOption('fund');assert.equal(await video.evaluate(v=>v.paused),true,'leaving a stage pauses its video');
 await page.locator('#fund video').evaluate(v=>v.dispatchEvent(new Event('error')));await page.locator('#fund [data-video-error]').waitFor();assert.equal(await page.locator('[data-guide-next]').isEnabled(),true,'video failure does not block instructions');
 assert.equal(await page.locator('video source[type="video/mp4"]').count(),steps.length);console.log('PASS real recording playback, inactive video pause, MP4 fallbacks and video-error instructions');await page.close();
 } finally {if(browser)await browser.close();if(server)await new Promise(r=>server.close(r));if(temporary)await fs.rm(temporary,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1});
