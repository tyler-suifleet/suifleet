// Offline regressions for recording helpers. No wallet, key or chain requests.
const assert = require('node:assert/strict');
const { appRequire } = require('../../scripts/frontend_test_utils.cjs');
const { chromium } = appRequire('playwright');
const { overlayScript } = require('../guide/recording_overlay.cjs');
const fs = require('node:fs');
const { helpers, reconnect, assertMutationLease } = require('../guide/browser_helpers.cjs');
assert.throws(() => assertMutationLease('private-fixture-token', () => { throw new Error('command argv: private-fixture-token'); }),
  error => error.message === 'testnet mutation lease refused' && !String(error.stack).includes('private-fixture-token'));
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage();
    await page.setContent('<meta http-equiv="Content-Security-Policy" content="default-src &apos;none&apos;; style-src &apos;self&apos;"><label>Deposit <input value="0.45"></label><wallet-ui></wallet-ui>');
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
    await page.evaluate(overlayScript);
    await helpers(page).caption("Optional captions must not be burned into recordings.");
    assert.equal(await page.locator("#guide-caption").count(),0);
    const cdp = await page.context().newCDPSession(page);
    const clip = { x: 395, y: 295, width: 40, height: 50, scale: 1 };
    const before = await cdp.send('Page.captureScreenshot', { format: 'png', clip });
    await page.mouse.move(400, 300, { steps: 10 });
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
    const after = await cdp.send('Page.captureScreenshot', { format: 'png', clip });
    assert.notEqual(after.data, before.data, 'cursor must be painted into captured pixels');
    assert.equal(await page.locator('#guide-pointer').evaluate(e => getComputedStyle(e).pointerEvents), 'none');
    const frames = [];
    cdp.on('Page.screencastFrame', event => { frames.push(event.data); void cdp.send('Page.screencastFrameAck', { sessionId: event.sessionId }); });
    await cdp.send('Page.startScreencast', { format: 'jpeg', quality: 92 });
    await page.mouse.move(450, 320, { steps: 10 });
    for (let attempt = 0; frames.length < 2 && attempt < 100; attempt++) await page.waitForTimeout(50);
    await cdp.send('Page.stopScreencast');
    assert.ok(frames.length >= 2, 'real compositor emits movement frames');
    if (process.env.GUIDE_OVERLAY_FRAME) fs.writeFileSync(process.env.GUIDE_OVERLAY_FRAME, Buffer.from(frames.at(-1), 'base64'));
    await helpers(page).type(page.getByLabel('Deposit'), '0.02');
    assert.ok(await page.locator('[id^="guide-click-"]').count(), 'actual pointerdown creates a click pulse');
    await page.waitForFunction(() => document.querySelectorAll('[id^="guide-click-"]').length === 0);
    assert.ok(await page.locator('#guide-pointer').isVisible(), 'cursor persists after click pulse expires');

    assert.equal(await page.getByLabel('Deposit').inputValue(), '0.02');
    await reconnect(page);
    assert.equal(await page.locator('#fleet-switcher').inputValue(), 'demo');
    // Reconnecting an already-connected page must preserve the chosen fleet.
    await reconnect(page);
    assert.equal(await page.locator('#fleet-switcher').inputValue(), 'demo');
    console.log('PASS: compositor-captured persistent pointer/movement and click pulses, nonintercepting overlay, controlled amount replacement, shadow-root wallet connection and selected-fleet reuse (offline)');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
