// Real browser coverage of the viewport layout and all explanatory text.
const fs = require('node:fs/promises');
const http = require('node:http');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.SUIFLEET_PLAYWRIGHT_MODULE || '../../dapp/node_modules/playwright');
(async () => {
  const root = path.resolve(process.argv[2]);
  const shots = process.argv[3];
  const policy = (await fs.readFile(path.join(root, '_headers'), 'utf8')).split('Content-Security-Policy: ')[1].split('\n')[0];
  const server = http.createServer(async (req, res) => {
    try {
      let name = new URL(req.url, 'http://local').pathname;
      if (name.endsWith('/')) name += 'index.html';
      const file = path.resolve(root, '.' + name);
      if (!file.startsWith(root + path.sep)) throw Error('outside root');
      const body = await fs.readFile(file);
      res.setHeader('Content-Type', ({'.html':'text/html','.js':'application/javascript','.css':'text/css','.svg':'image/svg+xml','.jpg':'image/jpeg','.woff2':'font/woff2','.webm':'video/webm','.mp4':'video/mp4','.vtt':'text/vtt'})[path.extname(file)] || 'application/octet-stream');
      res.setHeader('Content-Security-Policy', policy);
      res.setHeader('Accept-Ranges', 'bytes');
      const range = req.headers.range?.match(/^bytes=(\d+)-(\d*)$/);
      if (range) {
        const start = Number(range[1]), end = range[2] ? Math.min(Number(range[2]), body.length - 1) : body.length - 1;
        res.writeHead(206, {'Content-Range': `bytes ${start}-${end}/${body.length}`, 'Content-Length': end-start+1});
        res.end(body.subarray(start, end+1));
      } else { res.setHeader('Content-Length', body.length); res.end(body); }
    } catch { res.writeHead(404); res.end(); }
  });
  let browser;
  try {
    await new Promise(r => server.listen(0, '127.0.0.1', r));
    const origin = 'http://127.0.0.1:' + server.address().port;
    browser = await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE,headless:true,args:['--no-sandbox']});
    if (shots) await fs.mkdir(shots, {recursive:true});
    for (const [width,height] of [[1440,900],[1280,720],[390,844],[375,667],[320,568],[844,390],[640,360],[568,320]]) {
      const page = await browser.newPage({viewport:{width,height}}), errors = [];
      page.on('pageerror', e => errors.push(e.message));
      await page.goto(origin + '/how-it-works/', {waitUntil:'networkidle'});
      await page.evaluate(() => document.fonts.ready);
      assert.equal(await page.locator('video:visible').count(),1,'one full overview video');
      assert.equal(await page.locator('.guide-stage,[data-guide-select],.overview-caption,figcaption').count(),0,'no sections or video metadata');
      assert.ok(await page.evaluate(()=>document.documentElement.scrollHeight<=innerHeight+1&&document.documentElement.scrollWidth<=innerWidth),'overview must fit viewport');
      const video=page.locator('video:visible');
      assert.ok(await video.evaluate(v=>{const r=v.getBoundingClientRect();return r.height>=80&&r.bottom<=innerHeight}),'video stays visible');
      assert.equal(await video.locator('track[default]').count(),0,'captions remain optional');
      if(shots)await page.screenshot({path:path.join(shots,`how-${width}x${height}.png`)});
      assert.deepEqual(errors,[]);
      console.log(`PASS single-video overview ${width}x${height}: one visible movie, no sections, no overflow`);
      await page.close();
    }
  } finally { await browser?.close(); await new Promise(r => server.close(r)); }
})().catch(error => { console.error(error); process.exitCode=1; });
