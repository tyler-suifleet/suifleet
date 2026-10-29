// Offline illustration of the real exported console; no wallet or chain writes.
const fs = require('node:fs/promises');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require(process.env.SUIFLEET_PLAYWRIGHT_MODULE || '../../dapp/node_modules/playwright');
(async () => {
  const root = path.resolve(process.argv[2] || 'dapp/out');
  const server = http.createServer(async (req, res) => {
    let handle;
    try {
      let file = path.resolve(root, '.' + new URL(req.url, 'http://localhost').pathname);
      if (!file.startsWith(root + path.sep)) throw new Error('outside root');
      handle = await fs.open(file, 'r');
      if ((await handle.stat()).isDirectory()) {
        await handle.close();
        handle = undefined;
        file = path.join(file, 'index.html');
        handle = await fs.open(file, 'r');
      }
      const mime = {'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.woff2':'font/woff2'};
      res.setHeader('Content-Type', mime[path.extname(file)] || 'application/octet-stream');
      res.end(await handle.readFile());
    } catch { res.statusCode = 404; res.end(); }
    finally { if (handle) await handle.close(); }
  });
  let browser;
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const origin = `http://127.0.0.1:${server.address().port}`;
    browser = await chromium.launch({executablePath: process.env.CHROME_EXECUTABLE || undefined, headless:true, args:['--no-sandbox']});
    const page = await browser.newPage({viewport:{width:1280,height:800}, deviceScaleFactor:1});
    await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await page.goto(origin + '/get-started/', {waitUntil:'networkidle'});
    await page.getByRole('button', {name:'Connect Wallet', exact:true}).waitFor();
    await page.screenshot({path:path.join(__dirname,'../src/assets/console-start.png')});
    console.log('Captured signed-out console; external requests blocked.');
  } finally { if (browser) await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
