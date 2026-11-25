// Built marketing pages in Chromium; local routing only, no contact or wallet writes.
const fs = require('node:fs/promises'), path = require('node:path'), assert = require('node:assert/strict');
const { chromium } = require('../../dapp/node_modules/playwright');
(async () => {
  const root = path.resolve(process.argv[2] || '/tmp/sf275-site');
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || '/opt/google/chrome/chrome', headless: true, args: ['--no-sandbox'] });
  try {
    for (const width of [1440, 375]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } }), errors = [];
      page.on('pageerror', e => errors.push(e.message));
      await page.route('https://pricing.test/**', async route => {
        const pathname = new URL(route.request().url()).pathname;
        const file = path.join(root, pathname, pathname.endsWith('/') ? 'index.html' : '');
        try { await route.fulfill({ path: file }); } catch { await route.fulfill({ status: 404, body: '' }); }
      });
      await page.goto('https://pricing.test/pricing/');
      await page.getByRole('heading', { name: 'Example monthly platform bills' }).waitFor();
      const rows = page.getByRole('row');
      assert.match(await rows.filter({ hasText: '50 ×' }).innerText(), /\$4.00\s+\$49.00\s+\$5.00/);
      assert.match(await rows.filter({ hasText: '1,000 ×' }).innerText(), /\$80.00\s+\$80.00\s+\$100.00/);
      assert.match(await page.locator('main').innerText(), /Those rates alone are not an all-in total/);
      assert.equal(await page.getByText('Device capacity is the number', { exact: false }).isVisible(), false);
      await page.getByText('Optional detail: operating resources and network tokens', { exact: true }).click();
      assert.equal(await page.getByText('Device capacity is the number', { exact: false }).isVisible(), true);
      await page.getByRole('link', { name: 'Discuss a private pilot or quote' }).click();
      await page.getByRole('heading', { name: 'Private pilot and quote requests' }).waitFor();
      assert.equal(await page.locator('main a[href="mailto:hello@suifleet.io"]').count(), 1);
      assert.match(await page.locator('main').innerText(), /provide a written quote/);
      await page.goto('https://pricing.test/privacy/');
      assert.equal(await page.locator('main a[href="mailto:hello@suifleet.io"]').count(), 1);
      assert.match(await page.locator('main').innerText(), /private security reporting/);
      await page.goto('https://pricing.test/docs/');
      assert.equal(await page.getByRole('link', { name: 'the public documentation site', exact: true }).getAttribute('href'), 'https://docs.suifleet.io/');
      for (const url of ['pricing', 'contact', 'privacy', 'docs']) {
        await page.goto(`https://pricing.test/${url}/`);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${url} overflow at ${width}`);
      }
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}px: worked bills, optional token details, private pilot handoff, consistent privacy email, docs link and no overflow`);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
