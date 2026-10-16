// Exercise the built Next server and generated CSS; no public chain or wallet.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { appRequire } = require('./frontend_test_utils.cjs');
const { chromium } = appRequire('playwright');
(async () => {
  const server = spawn(process.execPath, [appRequire.resolve('next/dist/bin/next'), 'start', '--hostname', '127.0.0.1', '--port', '0'], { cwd: require('node:path').resolve(__dirname, '../dapp'), stdio: ['ignore', 'pipe', 'pipe'] });
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Next startup timeout')), 30000);
      server.once('exit', code => { clearTimeout(timeout); reject(new Error(`Next exited ${code}`)); });
      server.stdout.on('data', data => { const match = String(data).match(/http:\/\/127\.0\.0\.1:\d+/); if (match) { clearTimeout(timeout); resolve(match[0]); } });
    });
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ['--no-sandbox'] });
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    // Keep this regression isolated from public fullnodes and storage endpoints.
    await page.route('**/*', route => new URL(route.request().url()).origin === new URL(url).origin ? route.continue() : route.abort());
    for (const path of ['/', '/devices', '/deployments', '/components', '/firmware']) {
      const response = await page.goto(url + path);
      assert.equal(response.status(), 200);
      await page.locator('main').waitFor();
      await page.getByRole('button', { name: 'Connect Wallet', exact: true }).waitFor();
      assert.equal(await page.locator('aside').evaluate(e => getComputedStyle(e).width), '208px');
      assert.equal(await page.locator('main').evaluate(e => getComputedStyle(e).padding), '32px');
      assert.equal(await page.locator('.rounded').first().evaluate(e => getComputedStyle(e).borderRadius), '4px');
      assert.equal(await page.locator('body').evaluate(e => getComputedStyle(e).display), 'flex');
      assert.equal(await page.locator('body').evaluate(e => getComputedStyle(e).backgroundColor), 'rgb(240, 242, 245)');
    }
    assert.deepEqual(errors, []);
    console.log('PASS: production routes, hydration, wallet connect UI, generated layout/CSS and v3 radius compatibility; external requests blocked');
  } finally {
    await browser?.close();
    server.kill('SIGTERM');
    if (server.exitCode === null) await once(server, 'exit');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
