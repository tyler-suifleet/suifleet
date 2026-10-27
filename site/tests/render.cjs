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
    try {
      let file = path.resolve(root, '.' + new URL(req.url,'http://localhost').pathname);
      if (file !== root && !file.startsWith(root + path.sep)) throw new Error('outside root');
      if ((await fs.stat(file)).isDirectory()) file = path.join(file,'index.html');
      res.setHeader('Content-Type', {'.html':'text/html','.css':'text/css','.png':'image/png','.svg':'image/svg+xml'}[path.extname(file)] || 'application/octet-stream');
      res.end(await fs.readFile(file));
    } catch { res.statusCode=404;res.end(); }
  });
  let browser;
  try {
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const origin=`http://127.0.0.1:${server.address().port}`;
    browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE || undefined,headless:true,args:['--no-sandbox']});
    const external=[];
    for (const width of [375,1280]) {
      const page=await browser.newPage({viewport:{width,height:900}});
      page.on('request',req=>{if(new URL(req.url()).origin!==origin)external.push(req.url());});
      for (const item of config.pages) {
        const response=await page.goto(origin+item.path,{waitUntil:'networkidle'});
        assert.equal(response.status(),200,item.path);
        assert.equal(await page.locator('h1').count(),1,item.path);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${item.path} overflows ${width}`);
        assert.equal(await page.locator('img').evaluateAll(imgs=>imgs.every(i=>i.complete && i.naturalWidth>0)),true,item.path);
        await page.screenshot({path:path.join(shots,`${item.source}-${width}.png`),fullPage:true});
      }
      await page.close();
    }
    assert.deepEqual(external,[],'third-party resource requests');
    console.log(`PASS: ${config.pages.length*2} rendered pages, no overflow, missing images or external requests; screenshots ${shots}`);
  } finally { if(browser)await browser.close();await new Promise(resolve=>server.close(resolve)); }
})().catch(e=>{console.error(e);process.exitCode=1;});
