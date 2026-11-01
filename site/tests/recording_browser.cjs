// Offline regressions for recording helpers. No wallet, key or chain requests.
const assert = require('node:assert/strict');
const { appRequire } = require('../../scripts/frontend_test_utils.cjs');
const { chromium } = appRequire('playwright');
const { helpers, reconnect, assertMutationLease } = require('../guide/browser_helpers.cjs');
assert.throws(() => assertMutationLease('private-fixture-token', () => { throw new Error('command argv: private-fixture-token'); }),
  error => error.message === 'testnet mutation lease refused' && !String(error.stack).includes('private-fixture-token'));
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage();
    await page.setContent('<label>Deposit <input value="0.45"></label><wallet-ui></wallet-ui>');
    await page.evaluate(() => {
      const amount = document.querySelector('input');
      amount.addEventListener('input', () => { if (!amount.value) amount.value = '0.45'; });
      const shadow = document.querySelector('wallet-ui').attachShadow({ mode: 'open' });
      const connect = document.createElement('button'); connect.textContent = 'Connect Wallet'; shadow.append(connect);
      connect.onclick = () => {
        const choose = document.createElement('button'); choose.textContent = 'SuiFleet Guide Wallet'; shadow.append(choose);
        choose.onclick = () => {
          const fleet = document.createElement('select'); fleet.id = 'fleet-switcher';
          fleet.innerHTML = '<option value="demo">Demo fleet</option>'; document.body.append(fleet);
          connect.remove(); choose.remove();
        };
      };
    });
    await helpers(page).type(page.getByLabel('Deposit'), '0.02');
    assert.equal(await page.getByLabel('Deposit').inputValue(), '0.02');
    await reconnect(page);
    assert.equal(await page.locator('#fleet-switcher').inputValue(), 'demo');
    // Reconnecting an already-connected page must preserve the chosen fleet.
    await reconnect(page);
    assert.equal(await page.locator('#fleet-switcher').inputValue(), 'demo');
    console.log('PASS: controlled amount replacement, shadow-root wallet connection and selected-fleet reuse (offline)');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
