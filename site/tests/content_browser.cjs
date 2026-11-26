// Fast presentation checks: no unchanged-video playback, app build or chain access.
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.SUIFLEET_PLAYWRIGHT_MODULE||'../../dapp/node_modules/playwright');
(async()=>{
 const root=path.resolve(process.argv[2]);
 const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||'/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
 try{
  for(const [width,height] of [[320,568],[390,844],[844,390],[1440,900]]){
   const page=await browser.newPage({viewport:{width,height}}),errors=[];
   page.on('pageerror',e=>errors.push(e.message));
   await page.route('https://content.test/**',async route=>{
    const pathname=new URL(route.request().url()).pathname;
    const name=path.resolve(root,'.'+pathname+(pathname.endsWith('/')?'index.html':''));
    if(!name.startsWith(root+path.sep))return route.fulfill({status:404,body:''});
    try{await route.fulfill({path:name})}catch{await route.fulfill({status:404,body:''})}
   });
   for(const route of ['/','/how-it-works/','/guide/']){
    await page.goto('https://content.test'+route);await page.evaluate(()=>document.fonts.ready);
    if(await page.locator('[data-guide-player]').count())await page.waitForFunction(()=>document.body.classList.contains('guide-ready'));
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'horizontal overflow: '+route);
    const fixed=await page.evaluate(()=>document.body.matches('.guide-ready,.overview-page')||!!document.querySelector('.mobile-landing-links')&&innerWidth<=800);
    if(fixed)assert.ok(await page.evaluate(()=>document.documentElement.scrollHeight<=innerHeight+1),'viewport overflow: '+route);
    if(route==='/guide/'){
     assert.ok(await page.locator('[data-guide-next]').isVisible());
     await page.locator('[data-guide-next]').click();
     assert.ok(await page.locator('[data-guide-back]').isEnabled());
    }
   }
   assert.deepEqual(errors,[]);await page.close();
   console.log(`PASS focused content layout/navigation ${width}x${height}`);
  }
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
