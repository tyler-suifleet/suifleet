const fs = require('node:fs/promises'), path = require('node:path'), assert = require('node:assert/strict');
const { appRequire } = require('./frontend_test_utils.cjs');
const { build } = appRequire('esbuild'), { chromium } = appRequire('playwright');
(async () => {
  const root = path.resolve(__dirname, '..'), entry = path.join(__dirname, 'browser/billing-progress.jsx');
  const manifest = JSON.parse(await fs.readFile(path.join(root, 'manifests/fixture.localnet.json')));
  manifest.treasury_payee = '0x' + '9'.repeat(64);
  manifest.subscription_coins = { USDC: { coin_type: '0x' + '8'.repeat(64) + '::usdc::USDC', decimals: 6 }, SUI: { coin_type: '0x' + '0'.repeat(63) + '2::sui::SUI', decimals: 9 } };
  const result = await build({ entryPoints: [entry], bundle: true, write: false, platform: 'browser', jsx: 'automatic', nodePaths: [path.join(root, 'dapp/node_modules')], alias: { '@': path.join(root, 'dapp/src') }, plugins: [{ name: 'billing-fixture', setup(b) {
    b.onResolve({ filter: /^(@mysten\/dapp-kit-react|@\/hooks\/(useCurrentAccount|useFleetSelection|useFleetTransaction)|@\/lib\/subscription)$/ }, () => ({ path: entry }));
  } }], define: { 'process.env': '{}', 'process.env.NODE_ENV': '"production"', 'process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST': JSON.stringify(JSON.stringify(manifest)) } });
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || '/opt/google/chrome/chrome', headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 375, height: 667 } }), errors = [];
    page.setDefaultTimeout(5000);
    page.on('pageerror', e => errors.push(e.message));
    const cssPath = path.join(root, 'dapp/src/app/globals.css');
    const css = (await appRequire('postcss')([appRequire('@tailwindcss/postcss')({ base: path.join(root, 'dapp') })]).process(await fs.readFile(cssPath, 'utf8'), { from: cssPath })).css;
    await page.route('http://fleet.test/**', r => r.fulfill({ contentType: 'text/html', body: '<meta name="viewport" content="width=device-width, initial-scale=1"><style>' + css + '</style><body class="p-4"><div id="root"></div></body>' }));
    await page.goto('http://fleet.test'); await page.addScriptTag({ content: result.outputFiles[0].text });
    await page.getByLabel('Plan and payment coin').selectOption('0x' + '1'.repeat(64));
    async function visible(button, text) {
      const status = page.getByRole('status').filter({ hasText: text });
      await status.waitFor();
      // Feedback must be immediately next to the action and within the phone viewport.
      await button.scrollIntoViewIfNeeded();
      assert.equal(await button.evaluate(b => b.nextElementSibling?.getAttribute('role')), 'status');
      const box = await status.boundingBox(); assert(box.y >= 0 && box.y + box.height <= 667, `status outside phone: ${JSON.stringify(box)}`);
      assert.equal(await button.isDisabled(), true);
    }
    const prepay = page.getByRole('button', { name: 'Prepay from connected wallet in one transaction' });
    await prepay.click(); await visible(prepay, 'Checking payment details');
    await page.evaluate(() => window.billing.release()); // delayed preparePrepay
    await visible(prepay, 'Checking payment details');
    await page.waitForFunction(() => window.billing.calls === 1);
    await page.evaluate(() => window.billing.release()); await visible(prepay, 'Approve this billing action');
    await prepay.dispatchEvent('click'); assert.equal(await page.evaluate(() => window.billing.calls), 1);
    await page.evaluate(() => window.billing.release()); await visible(prepay, 'Do not pay again');
    await page.evaluate(() => window.billing.release()); await page.getByRole('status').filter({ hasText: 'Confirmed.' }).waitFor();
    assert.match(await page.getByRole('status').innerText(), /Submitted digest: fixture-digest/);
    const collect = page.getByRole('button', { name: 'Apply due prepaid payment' });
    await collect.click(); await visible(collect, 'Checking payment details');
    assert.doesNotMatch(await page.getByRole('status').innerText(), /Submitted digest|fixture-digest/);
    await page.evaluate(() => window.billing.release()); await visible(collect, 'Approve this billing action');
    assert.doesNotMatch(await page.getByRole('status').innerText(), /Submitted digest|fixture-digest/);
    await page.evaluate(() => window.billing.release()); await visible(collect, 'Do not pay again');
    assert.match(await page.getByRole('status').innerText(), /Submitted digest: fixture-digest/);
    await page.evaluate(() => { window.billing.fail = true; window.billing.release(); });
    await page.getByRole('status').filter({ hasText: 'Confirmation unavailable' }).waitFor();
    assert.match(await page.getByRole('status').innerText(), /Check a submitted digest before retrying/);
    assert.deepEqual(errors, []);
    console.log('PASS: actual billing page at 375x667; delayed preparation/validation/wallet/confirmation, adjacent visible status for prepay and collection, duplicate-submit guard, success and uncertain-confirmation retry warning; fixture transactions only');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
