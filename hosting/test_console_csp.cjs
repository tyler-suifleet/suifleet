// Serve the prepared console without _headers, matching the provider limitation.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {appRequire} = require('../scripts/frontend_test_utils.cjs');
const {chromium} = appRequire('playwright');
(async () => {
  const root = path.resolve(process.argv[2]);
  const server = http.createServer((req, res) => {
    let file = path.resolve(root, '.' + new URL(req.url, 'http://localhost').pathname);
    if (!file.startsWith(root + path.sep) && file !== root) {res.writeHead(403).end(); return;}
    if (fs.existsSync(file) && fs.statSync(file).isDirectory()) file = path.join(file, 'index.html');
    if (!fs.existsSync(file)) {res.writeHead(404).end(); return;}
    const types = {'.html':'text/html', '.js':'text/javascript', '.css':'text/css', '.json':'application/json', '.svg':'image/svg+xml', '.woff2':'font/woff2'};
    res.setHeader('Content-Type', types[path.extname(file)] || 'application/octet-stream');
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE, headless:true, args:['--no-sandbox']});
    const page = await browser.newPage(), errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto('http://127.0.0.1:' + server.address().port + '/get-started/', {waitUntil:'networkidle'});
    await page.locator('h1').first().waitFor();
    assert.ok(await page.locator('meta[http-equiv="Content-Security-Policy"]').count());
    await page.evaluate(() => {
      window.cspFailures = [];
      document.addEventListener('securitypolicyviolation', e => window.cspFailures.push(e.effectiveDirective));
      const script = document.createElement('script');
      script.textContent = 'window.unreviewedScriptRan = true';
      document.head.append(script);
    });
    await page.waitForFunction(() => window.cspFailures.includes('script-src-elem'));
    assert.equal(await page.evaluate(() => window.unreviewedScriptRan), undefined);
    assert.deepEqual(errors, []);
    console.log('PASS prepared console hydrates without response CSP; unreviewed inline script is blocked');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
