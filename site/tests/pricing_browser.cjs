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
      await page.getByRole('heading', { name: 'Pay per device.' }).waitFor();
      assert.equal(await page.locator('.plan').count(), 3);
      assert.match(await page.locator('.plan.featured').innerText(), /Essentials[\s\S]*\$0\.08/);
      assert.doesNotMatch(await page.locator('main').innerText(), /Managed|At-cost|at cost|design partner/i);
      const detail = page.locator('.estimates-body');
      assert.equal(await detail.isVisible(), false);
      await page.getByText('See price estimates', { exact: true }).click();
      assert.equal(await detail.isVisible(), true);
      assert.match(await detail.innerText(), /1,000\s+\$80\s+\$180\s+\$200/);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'expanded estimates overflow the page');
      await page.getByText('Hide price estimates', { exact: true }).click();
      assert.equal(await detail.isVisible(), false);
      await page.locator('.price-band').getByRole('link', { name: 'Talk to us' }).click();
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
      console.log(`PASS ${width}px: three configurations, fleet-size estimates, expandable detail, private pilot handoff, consistent privacy email, docs link and no overflow`);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
