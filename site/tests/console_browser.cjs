// Offline HTTPS routing fixture: no listening port, provider or wallet session.
const { chromium } = require('../../dapp/node_modules/playwright');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const root = path.resolve(process.argv[2]);
  const browser = await chromium.launch({executablePath: process.env.CHROME_EXECUTABLE || '/opt/google/chrome/chrome', args: ['--no-sandbox']});
  try {
    const page = await browser.newPage();
    await page.addInitScript(() => {
      window.policyViolations = [];
      document.addEventListener('securitypolicyviolation', e => window.policyViolations.push([e.effectiveDirective, e.blockedURI]));
    });
    const headers = Object.fromEntries(fs.readFileSync(path.join(root, '_headers'), 'utf8').split('\n').filter(l => l.startsWith('  ')).map(l => {const i=l.indexOf(':'); return [l.slice(0,i).trim(),l.slice(i+1).trim()]}));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== 'https://app.suifleet.io') return route.abort();
      let name = decodeURIComponent(url.pathname);
      if (name.endsWith('/')) name += 'index.html';
      const file = path.resolve(root, '.' + name);
      if (!file.startsWith(root + '/') || !fs.existsSync(file)) return route.fulfill({status:404,body:'fixture missing'});
      const types={'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json','.svg':'image/svg+xml'};
      await route.fulfill({body:fs.readFileSync(file),headers:{...headers,'Content-Type':types[path.extname(file)] || 'text/plain'}});
    });
    await page.goto('https://app.suifleet.io/', {waitUntil:'networkidle'});
    await page.getByRole('link', {name:'Get started',exact:true}).first().waitFor();
    const violations = await page.evaluate(() => window.policyViolations);
    const scripts = violations.filter(([directive]) => directive.startsWith('script-src'));
    if (scripts.length) throw new Error(JSON.stringify(scripts));
    console.log('Offline console rendered with generated CSP; no script policy violations. No wallet/provider acceptance.');
  } finally {await browser.close();}
})().catch(e => {console.error(e);process.exitCode=1;});
