// Read-only acceptance of every published guide player. No wallet or chain calls.
const fs=require('node:fs/promises'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {appRequire}=require('../../scripts/frontend_test_utils.cjs'),{chromium}=appRequire('playwright');
(async()=>{
 let origin=process.argv[2],server;assert.ok(origin,'Supply the actual HTTPS origin or a built-site directory');
 if(!origin.startsWith('https://')){
  const root=path.resolve(origin),policy=(await fs.readFile(path.join(root,'_headers'),'utf8')).split('Content-Security-Policy: ')[1].split('\n')[0];
  server=http.createServer(async(req,res)=>{try{
   let name=new URL(req.url,'http://local').pathname;if(name.endsWith('/'))name+='index.html';const file=path.resolve(root,'.'+name);if(!file.startsWith(root+path.sep))throw Error('outside root');const data=await fs.readFile(file);
   res.setHeader('Content-Type',({'.html':'text/html','.css':'text/css','.js':'application/javascript','.woff2':'font/woff2','.svg':'image/svg+xml','.jpg':'image/jpeg','.png':'image/png','.webm':'video/webm','.mp4':'video/mp4','.vtt':'text/vtt'})[path.extname(file)]||'application/json');res.setHeader('Content-Security-Policy',policy);res.setHeader('Accept-Ranges','bytes');
   const range=req.headers.range?.match(/^bytes=(\d+)-(\d*)$/);if(range){const start=Number(range[1]),end=range[2]?Math.min(Number(range[2]),data.length-1):data.length-1;if(start>end){res.writeHead(416);res.end();return}res.writeHead(206,{'Content-Range':`bytes ${start}-${end}/${data.length}`,'Content-Length':end-start+1});res.end(data.subarray(start,end+1))}else{res.setHeader('Content-Length',data.length);res.end(data)}
  }catch{res.writeHead(404);res.end()}});await new Promise(r=>server.listen(0,'127.0.0.1',r));origin=`http://127.0.0.1:${server.address().port}`;
 }
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
   if(manifest.tutorial_production){
    const cues=clip.narration.cues,action=cues.find(c=>c.kind.startsWith('action-'));
    const phases=await v.evaluate(async(v,times)=>{
     const frames=[];let dark=0;
     for(const [i,time]of times.entries()){
      await new Promise(r=>{v.addEventListener('seeked',r,{once:true});v.currentTime=time});await v.play();await new Promise(r=>setTimeout(r,250));v.pause();
      const canvas=document.createElement('canvas');canvas.width=v.videoWidth;canvas.height=v.videoHeight;const c=canvas.getContext('2d');c.drawImage(v,0,0);
      if(i===1){const d=c.getImageData(70,170,170,470).data;for(let n=0;n<d.length;n+=4)if(d[n]<90&&d[n+1]<90&&d[n+2]<100)dark++;dark/=d.length/4}
      const small=document.createElement('canvas');small.width=160;small.height=100;const x=small.getContext('2d');x.drawImage(canvas,0,0,160,100);frames.push([...x.getImageData(0,0,160,100).data]);
     }
     const diff=(a,b)=>{let n=0;for(let i=0;i<a.length;i+=4)if(Math.abs(a[i]-b[i])+Math.abs(a[i+1]-b[i+1])+Math.abs(a[i+2]-b[i+2])>60)n++;return n/(a.length/4)};
     return {dark,introBody:diff(frames[0],frames[1]),bodyOutcome:diff(frames[1],frames[2])};
    },[.8,action.start+1,cues.at(-1).start+.8]);
    assert.ok(phases.dark>.35,'actual app viewport missing: '+clip.step+' '+JSON.stringify(phases));
    assert.ok(phases.introBody>.08&&phases.bodyOutcome>.08,'lesson phases did not change: '+clip.step);
   }

  }
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.deepEqual(errors,[]);
  console.log('PASS every guide: captions off by default/on/off, complete narration cues, corrected video dimensions and versioned live media:',width);await page.close();
 }}finally{await browser.close();if(server)await new Promise(r=>server.close(r))}
})().catch(e=>{console.error(e);process.exitCode=1});
