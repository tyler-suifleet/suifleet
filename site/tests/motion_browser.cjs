// Inspect actual participating actors, connected transfers, readable labels and scene development.
const fs=require('node:fs/promises'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {appRequire}=require('../../scripts/frontend_test_utils.cjs'),{chromium}=appRequire('playwright');
const expected={fleet:['console','device','sui'],approve:['console','sui','wallet'],publish:['console','sui','walrus'],poll:['device','sui'],install:['device','walrus'],report:['console','device','sui'],overview:['console','device','sui','walrus']};
(async()=>{
 const root=path.resolve(__dirname,'../..'),story=JSON.parse(await fs.readFile(path.join(root,'site/overview/storyboard.json')));
 const server=http.createServer(async(req,res)=>{try{const file=path.resolve(root,'.'+new URL(req.url,'http://local').pathname);if(!file.startsWith(root+path.sep))throw Error();res.setHeader('Content-Type',file.endsWith('.html')?'text/html':file.endsWith('.svg')?'image/svg+xml':'font/woff2');res.end(await fs.readFile(file))}catch{res.writeHead(404);res.end()}});let browser;
 try{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE,headless:true,args:['--no-sandbox']});
  const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(`http://127.0.0.1:${server.address().port}/site/overview/scene.html`);await page.evaluate(()=>window.ready);
  for(const [width,height] of [[1280,720],[720,1280]]){
   for(let index=0;index<story.scenes.length;index++){
    const scene={...story.scenes[index],duration:22};let ratio=0;
    for(const localTime of [2,7,14,19]){
     const frame=await page.evaluate(async args=>{
      await window.renderFrame(args);const c=document.querySelector('canvas').getContext('2d');
      const pixels=c.getImageData(40,210,args.width-80,args.height-270).data;let ratio=0;if(args.localTime===2)window.motionSample=pixels;if(args.localTime===7){let changed=0;for(let i=0;i<pixels.length;i+=4)if(Math.abs(pixels[i]-window.motionSample[i])+Math.abs(pixels[i+1]-window.motionSample[i+1])+Math.abs(pixels[i+2]-window.motionSample[i+2])>70)changed++;ratio=changed/(pixels.length/4)}return {actors:window.renderedActors,transfers:window.renderedTransfers,labels:window.renderedLabels,ratio,image:document.querySelector('canvas').toDataURL('image/png').split(',')[1]};
     },{width,height,scene,localTime,index,total:7});
     assert.deepEqual(Object.keys(frame.actors).sort(),expected[scene.id]);
     for(const transfer of frame.transfers){assert.ok(frame.actors[transfer.from]&&frame.actors[transfer.to],'transfer disconnected from participating component');assert.notEqual(transfer.from,transfer.to);}
     assert.ok(frame.transfers.length,'scene has no connected action');
     for(const text of frame.labels)assert.ok(text.left>=0&&text.right<=width&&text.top>=0&&text.bottom<height-35,`${scene.id} ${width} t=${localTime}: clipped text ${JSON.stringify(text)}`);
     const a=frame.actors;
     if(a.console&&a.sui)assert.ok(a.console.x<a.sui.x,'console/Sui orientation changed');
     if(a.device&&a.walrus)assert.ok(a.device.x<a.walrus.x,'device/Walrus orientation changed');
     if(a.sui&&a.walrus)assert.ok(a.sui.y<a.walrus.y,'records/storage orientation changed');
     if(a.console&&a.device)assert.ok(a.console.y<a.device.y,'console/device orientation changed');
     if(localTime===7)ratio=frame.ratio;
     if(process.env.MOTION_FRAME_DIR){await fs.mkdir(process.env.MOTION_FRAME_DIR,{recursive:true});await fs.writeFile(path.join(process.env.MOTION_FRAME_DIR,`${width}-${scene.id}-${localTime}.png`),Buffer.from(frame.image,'base64'));}
    }
    assert.ok(ratio>.002,`${scene.id} ${width}: insufficient illustrated motion ${ratio}`);
    console.log('PASS integrated actor/transfer/orientation/text/motion',scene.id,width,(ratio*100).toFixed(2)+'%');
   }
  }
  assert.deepEqual(errors,[]);
 }finally{await browser?.close();await new Promise(r=>server.close(r))}
})().catch(e=>{console.error(e);process.exitCode=1});
