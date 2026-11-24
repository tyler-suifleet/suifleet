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
      await page.goto(origin + '/how-it-works/#overview', {waitUntil:'networkidle'});
      await page.waitForFunction(() => document.body.classList.contains('guide-ready'));
      await page.evaluate(() => document.fonts.ready);
      const expected = await page.locator('.guide-stage').evaluateAll(stages => Object.fromEntries(stages.flatMap(stage => [...stage.querySelectorAll('[data-guide-instruction]')].map((item,i) => [`${stage.id}/${i}`, item.textContent.trim().replace(/\s+/g,' ')]))));
      if (shots) await page.screenshot({path:path.join(shots, `how-${width}x${height}.png`)});
      const gathered = new Map(); let count = 0;
      while (!(await page.locator('[data-guide-complete]').isVisible())) {
        const value = await page.evaluate(() => {
          const stage = [...document.querySelectorAll('.guide-stage')].find(s => !s.hidden);
          const copy = stage.querySelector('.guide-copy');
          const rect = document.querySelector('.guide-controls').getBoundingClientRect();
          return {key:`${stage.id}/${stage.dataset.instruction}`, text:copy.textContent.trim(), part:Number(stage.dataset.part),
            page:[document.documentElement.scrollWidth,document.documentElement.scrollHeight],
            fits:copy.clientHeight>0 && copy.scrollHeight<=copy.clientHeight && copy.scrollWidth<=copy.clientWidth,
            controls:[rect.top,rect.bottom]};
        });
        assert.ok(value.page[0]<=width && value.page[1]<=height, `document overflow ${width}x${height}: ${JSON.stringify(value)}`);
        assert.ok(value.fits, `clipped explanation ${width}x${height}: ${JSON.stringify(value)}`);
        assert.ok(value.controls[0]>=0 && value.controls[1]<=height, 'navigation outside viewport');
        if (value.part===0) gathered.set(value.key, []);
        gathered.get(value.key).push(value.text);
        const forward = page.locator('.guide-stage:not([hidden]) [data-guide-forward]');
        await (await forward.isDisabled() ? page.locator('[data-guide-next]') : forward).click();
        assert.ok(++count<500, 'navigation did not terminate');
      }
      assert.equal(gathered.size, Object.keys(expected).length);
      for (const [key,text] of Object.entries(expected)) assert.equal(gathered.get(key).join(' '), text, `lost text ${key}`);
      await page.getByLabel('Overview section').selectOption('first-update');
      await page.locator('#first-update').waitFor();
      await page.goBack();
      await page.locator('[data-guide-complete]').waitFor();
      await page.getByLabel('Overview section').selectOption('big-picture');
      await page.getByText('Read the complete narration', {exact:true}).click();
      assert.equal(await page.locator('.overview-transcript li:visible').count(),9);
      assert.ok(await page.evaluate(() => document.documentElement.scrollHeight<=innerHeight), 'transcript expands document');
      await page.getByText('Read the complete narration', {exact:true}).click();
      if (width===390 || width===1440) {
        const video = page.locator('#overview video:visible');
        assert.equal(await video.count(), 1);
        assert.equal(await video.evaluate(v => v.paused), true);
        assert.equal(await video.locator('track').evaluate(t => t.default),false);
        await video.evaluate(async v => { v.muted=true; try { await v.play(); } catch (error) { if(error.name!=='AbortError' || !v.src.startsWith('blob:')) throw error; } });
        await page.waitForFunction(() => [...document.querySelectorAll('#overview video')].some(v => getComputedStyle(v).display!=='none' && v.currentTime>.2));
        await video.evaluate(v => {v.textTracks[0].mode='showing';v.pause();});
        assert.equal(await video.evaluate(v => v.textTracks[0].mode),'showing');
      }
      await page.mouse.wheel(0,800);
      assert.equal(await page.evaluate(() => scrollY),0);
      assert.deepEqual(errors, []);
      console.log(`PASS How it works ${width}x${height}: all ${gathered.size} explanations reachable, ${count} pages, no document/text overflow`);
      await page.close();
    }
  } finally { await browser?.close(); await new Promise(r => server.close(r)); }
})().catch(error => { console.error(error); process.exitCode=1; });
