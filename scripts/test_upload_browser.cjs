// Offline real browser: actual upload page/transport, controlled publisher and wallet.
const fs = require('node:fs/promises'), path = require('node:path'), os = require('node:os'), http = require('node:http');
const assert = require('node:assert/strict');
const { appRequire } = require('./frontend_test_utils.cjs');
const { build } = appRequire('esbuild'), { chromium } = appRequire('playwright');
(async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'suifleet-upload-browser-'));
  let server, browser;
  try {
    const fixture = await fs.readFile(path.join(__dirname, '../manifests/fixture.localnet.json'), 'utf8');
    const entry = path.join(__dirname, 'browser/upload-acceptance.jsx');
    await build({ entryPoints: [entry], outfile: path.join(directory, 'app.js'), bundle: true, platform: 'browser', jsx: 'automatic',
      alias: { '@': path.resolve(__dirname, '../dapp/src') }, nodePaths: [path.resolve(__dirname, '../dapp/node_modules')],
      plugins: [{ name: 'acceptance-hooks', setup(builder) {
        builder.onResolve({ filter: /^(@mysten\/dapp-kit-react|@\/hooks\/(useFleetTransaction|useDevices|useGroups|useAccessPolicies))$/ }, () => ({ path: entry }));
      } }],
      define: { 'process.env': '{}', 'process.env.NODE_ENV': '"development"', 'process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST': JSON.stringify(fixture), 'process.env.NEXT_PUBLIC_SEAL_KEY_SERVERS': 'undefined', 'process.env.NEXT_PUBLIC_SEAL_THRESHOLD': 'undefined', 'process.env.NEXT_PUBLIC_WALRUS_EPOCHS': '"5"', 'process.env.NEXT_PUBLIC_WALRUS_MAX_COST_FROST': 'undefined' }, logLevel: 'warning' });
    server = http.createServer(async (request, response) => {
      response.setHeader('Content-Type', request.url === '/app.js' ? 'text/javascript' : 'text/html');
      response.end(request.url === '/app.js' ? await fs.readFile(path.join(directory, 'app.js')) : '<!doctype html><div id="root"></div><script src="/app.js"></script>');
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ['--no-sandbox'] });
    const page = await browser.newPage(), errors = []; page.setDefaultTimeout(10000);
    page.on('pageerror', error => { errors.push(error.message); console.error(error.message); });
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.getByPlaceholder('my-app', { exact: true }).fill('upload-test');
    await page.getByPlaceholder('1.0.0', { exact: true }).fill('1.0.0');
    await page.getByRole('radio').nth(1).check();
    await page.getByRole('checkbox').first().check();
    const upload = () => page.locator('input[type=file]').setInputFiles({ name: 'test.bin', mimeType: 'application/octet-stream', buffer: Buffer.from('test artifact') });
    await upload();
    await page.waitForFunction(() => window.uploadAcceptance.count() === 1);
    await page.evaluate(() => window.uploadAcceptance.progress(0, 5, 10));
    await page.waitForFunction(() => document.querySelector('[role=progressbar]').getAttribute('aria-valuenow') === '50');
    await page.evaluate(() => window.uploadAcceptance.progress(0, 5, null));
    assert.equal(await page.getByRole('progressbar').getAttribute('aria-valuenow'), null);
    await page.evaluate(() => window.uploadAcceptance.sent(0));
    assert.match(await page.getByRole('progressbar').innerText(), /waiting for Walrus/);
    await page.evaluate(() => window.uploadAcceptance.finish(0));
    await page.getByRole('button', { name: 'Continue to target groups' }).click();
    await page.getByRole('checkbox', { name: /Make this the groups/ }).uncheck();
    await page.getByRole('checkbox', { name: /Acceptance device/ }).check();
    const deploy = page.getByRole('button', { name: 'Deploy to 1 target', exact: true });
    // Multiple events before a render must still acquire exactly one upload.
    await deploy.evaluate(button => { button.click(); button.click(); button.click(); });
    await page.waitForFunction(() => window.uploadAcceptance.count() === 2);
    assert.equal(await page.getByRole('button', { name: 'Uploading recipe…', exact: true }).isDisabled(), true);
    assert.equal(await page.evaluate(() => window.uploadAcceptance.walletCalls()), 0);
    await page.evaluate(() => window.uploadAcceptance.finish(1, 503));
    await page.waitForFunction(() => window.uploadAcceptance.alerts().length === 1);
    assert.equal(await page.evaluate(() => window.uploadAcceptance.walletCalls()), 0);
    await deploy.click();
    await page.waitForFunction(() => window.uploadAcceptance.count() === 3);
    await page.evaluate(() => window.uploadAcceptance.finish(2));
    await page.waitForFunction(() => window.uploadAcceptance.walletCalls() === 1);
    assert.equal(await page.getByRole('button', { name: 'Awaiting confirmation…', exact: true }).isDisabled(), true);
    await page.evaluate(() => window.uploadAcceptance.reject());
    await page.waitForFunction(() => window.uploadAcceptance.alerts().length === 2);
    await deploy.click();
    await page.waitForFunction(() => window.uploadAcceptance.count() === 4);
    await page.evaluate(() => window.uploadAcceptance.finish(3));
    await page.waitForFunction(() => window.uploadAcceptance.walletCalls() === 2);
    await page.evaluate(() => window.uploadAcceptance.succeed());
    await page.getByText('Deployment created', { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => window.uploadAcceptance.count()), 4);
    assert.deepEqual(errors, []);
    console.log('PASS: real upload UI progress/unknown total/processing; duplicate clicks yield one upload; upload failure and wallet cancellation allow explicit retry; success shown without React key warnings');
  } finally {
    if (browser) await browser.close();
    if (server) await new Promise(resolve => server.close(resolve));
    await fs.rm(directory, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
