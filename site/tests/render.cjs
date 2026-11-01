// Optional rendered smoke check: node site/tests/render.cjs [build] [screenshots].
// Uses the console's installed Playwright; site build and offline CI need no Node.
const fs = require('node:fs/promises'), path = require('node:path'), http = require('node:http');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.SUIFLEET_PLAYWRIGHT_MODULE || '../../dapp/node_modules/playwright');
(async () => {
  const root = path.resolve(process.argv[2] || 'site/dist'), shots = path.resolve(process.argv[3] || '/tmp/sf172-render');
  const config = JSON.parse(await fs.readFile(path.join(__dirname,'../src/site.json')));
  await fs.mkdir(shots,{recursive:true});
  const server = http.createServer(async (req,res) => {
    let handle;
    try {
      let file = path.resolve(root, '.' + new URL(req.url,'http://localhost').pathname);
      if (file !== root && !file.startsWith(root + path.sep)) throw new Error('outside root');
      handle = await fs.open(file, 'r');
      if ((await handle.stat()).isDirectory()) {
        await handle.close();
        handle = undefined;
        file = path.join(file, 'index.html');
        handle = await fs.open(file, 'r');
      }
      res.setHeader('Content-Type', {'.html':'text/html','.css':'text/css','.png':'image/png','.svg':'image/svg+xml','.woff2':'font/woff2'}[path.extname(file)] || 'application/octet-stream');
      res.end(await handle.readFile());
    } catch { res.statusCode=404;res.end(); }
    finally { if (handle) await handle.close(); }
  });
  let browser;
  try {
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const origin=`http://127.0.0.1:${server.address().port}`;
    browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE || undefined,headless:true,args:['--no-sandbox']});
    const external=[], errors=[];
    for (const width of [375,390,1280]) {
      const page=await browser.newPage({viewport:{width,height:900}});
      page.on('pageerror',error=>errors.push(error.message));
      page.on('request',req=>{if(new URL(req.url()).origin!==origin)external.push(req.url());});
      for (const item of config.pages) {
        const response=await page.goto(origin+item.path,{waitUntil:'networkidle'});
        assert.equal(response.status(),200,item.path);
        assert.equal(await page.locator('h1').count(),1,item.path);
        const fonts = await page.evaluate(async()=>{const loaded=await document.fonts.load('400 16px Inter');return loaded.length>0 && document.fonts.check('400 16px Inter');});
        assert.equal(fonts,true,`${item.path}: local font did not load`);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${item.path} overflows ${width}`);
        assert.equal(await page.locator('img').evaluateAll(imgs=>imgs.every(i=>i.complete && i.naturalWidth>0)),true,item.path);
        await page.screenshot({path:path.join(shots,`${item.source}-${width}.png`),fullPage:true});
      }
      await page.goto(origin+'/',{waitUntil:'networkidle'});
      await page.keyboard.press('Tab');
      assert.equal(await page.locator('.skip').evaluate(el=>el===document.activeElement),true,'skip link is first keyboard target');
      const lastNav=page.locator('nav a').last(); await lastNav.focus();
      assert.equal(await lastNav.evaluate(el=>{const b=el.getBoundingClientRect();return b.left>=0 && b.right<=innerWidth;}),true,'focused navigation stays reachable on mobile');
      await page.close();
    }
    assert.deepEqual(external,[],'third-party resource requests');
    assert.deepEqual(errors,[],'browser runtime errors');
    console.log(`PASS: ${config.pages.length*3} rendered pages, no overflow, missing images or external requests; screenshots ${shots}`);
  } finally { if(browser)await browser.close();await new Promise(resolve=>server.close(resolve)); }
})().catch(e=>{console.error(e);process.exitCode=1;});
