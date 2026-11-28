// Exercise mobile navigation without site JavaScript and check the customer path.
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('../../dapp/node_modules/playwright');
(async()=>{
 const root=path.resolve(process.argv[2]),shots=process.argv[3];
 const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||'/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
 try {
  if(shots)await fs.mkdir(shots,{recursive:true});
  for(const width of [320,390,560,768,800,960,961,1440]) {
   const context=await browser.newContext({viewport:{width,height:844},javaScriptEnabled:false});
   const page=await context.newPage(),errors=[];page.on('pageerror',error=>errors.push(error.message));
   await page.route('https://landing.test/**',async route=>{
    const pathname=new URL(route.request().url()).pathname;
    try{await route.fulfill({path:path.join(root,pathname,pathname.endsWith('/')?'index.html':'')})}
    catch{await route.fulfill({status:404,body:''})}
   });
   await page.goto('https://landing.test/');await page.evaluate(()=>document.fonts.ready);
   assert.equal(await page.locator('script').count(),0,'landing navigation must not depend on a site script');
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'document overflow at '+width);
   assert.equal(await page.locator('.hero').getByRole('link',{name:'Start with one device',exact:true}).getAttribute('href'),'/guide/');
   assert.equal(await page.locator('.hero').getByRole('link',{name:'Watch how it works →',exact:true}).getAttribute('href'),'/how-it-works/');
   if(width<=960) {
    const summary=page.locator('.mobile-menu summary');await summary.focus();await page.keyboard.press('Enter');
    const nav=page.getByRole('navigation',{name:'Mobile navigation'});assert.equal(await nav.isVisible(),true);
    assert.equal(await page.getByRole('navigation',{name:'Main',exact:true}).isVisible(),false);
    assert.equal(await nav.getByRole('link').count(),6);
    for(const element of [summary,...await nav.getByRole('link').all(),page.locator('.header-cta')]) {
     assert.ok(await element.evaluate(e=>{const r=e.getBoundingClientRect();return r.height>=44&&r.left>=0&&r.right<=innerWidth}),`touch target or menu overflow at ${width}`);
    }
    await page.keyboard.press('Tab');assert.equal(await nav.getByRole('link').first().evaluate(e=>e===document.activeElement),true);
    await summary.focus();await page.keyboard.press('Enter');assert.equal(await nav.isVisible(),false);
    if(width<=560) {
     assert.ok(await page.locator('.site-header').evaluate(e=>e.getBoundingClientRect().height<=90),'header should fit one row');
     for(const selector of ['.hero .lead','.copy .lead','.closing .lead'])
      for(const item of await page.locator(selector).all())assert.ok(await item.evaluate(e=>parseFloat(getComputedStyle(e).fontSize)>=15),selector+' body copy too small');
    }
    if(shots)await page.screenshot({path:path.join(shots,`landing-${width}.png`),fullPage:true});
    await summary.click();await nav.getByRole('link',{name:'Pricing',exact:true}).click();
    await page.getByRole('heading',{name:'Pay per device.',exact:true}).waitFor();
   } else {
    assert.equal(await page.locator('.mobile-menu').isVisible(),false);
    assert.equal(await page.getByRole('navigation',{name:'Main',exact:true}).isVisible(),true);
    if(shots)await page.screenshot({path:path.join(shots,`landing-${width}.png`),fullPage:true});
   }
   assert.deepEqual(errors,[]);console.log(`PASS landing ${width}px: navigation without JavaScript, keyboard/touch targets, customer path and responsive layout`);
   await context.close();
  }
  for(const [width,height] of [[320,568],[390,664],[667,375]]) {
   const context=await browser.newContext({viewport:{width,height},javaScriptEnabled:false});
   const page=await context.newPage();
   await page.route('https://landing.test/**',async route=>{
    const pathname=new URL(route.request().url()).pathname;
    try{await route.fulfill({path:path.join(root,pathname,pathname.endsWith('/')?'index.html':'')})}
    catch{await route.fulfill({status:404,body:''})}
   });
   await page.goto('https://landing.test/');await page.evaluate(()=>document.fonts.ready);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollHeight<=innerHeight+1),'phone homepage must fit one viewport');
   for(const selector of ['.hero h1','.hero .lead','.hero .cta-row','.hero .tile','.mobile-landing-links'])
    assert.ok(await page.locator(selector).evaluate(e=>{const r=e.getBoundingClientRect();return r.height>0&&r.top>=0&&r.bottom<=innerHeight+1}),selector+' must be visible without scrolling');
   await page.getByRole('link',{name:'Explore product',exact:true}).click();
   await page.getByRole('heading',{name:'Built around your fleet.',exact:true}).waitFor();
   assert.equal(await page.getByRole('heading',{name:'Roll out in stages.',exact:true}).isVisible(),true,'longer explanations remain accessible');
   await context.close();console.log(`PASS single-screen landing ${width}x${height} and product details navigation`);
  }
 } finally {await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
