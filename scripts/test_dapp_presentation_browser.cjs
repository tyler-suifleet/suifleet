// Exercises the exported console with a disposable browser and local-only server.
// No wallet, network fixtures, chain submissions or user's browser profile.
const fs = require('node:fs/promises'), path = require('node:path'), os = require('node:os'), http = require('node:http');
const assert = require('node:assert/strict');
const { appRequire } = require('./frontend_test_utils.cjs');
const { build } = appRequire('esbuild'), { chromium } = appRequire('playwright');
const root = path.resolve(__dirname, '..'), output = path.join(root, 'dapp/out');
const routes = ['/', '/get-started/', '/devices/', '/services/', '/data/', '/enrollment/', '/deployments/', '/rollouts/', '/components/', '/firmware/', '/access/', '/handover/', '/settings/', '/budget/'];
(async () => {
  // Every app route, so a new page cannot skip the signed-out check.
  const pages = (await fs.readdir(path.join(root, 'dapp/src/app'), { withFileTypes: true }))
    .filter(entry => entry.isDirectory() && require('node:fs').existsSync(path.join(root, 'dapp/src/app', entry.name, 'page.tsx'))).map(entry => `/${entry.name}/`);
  assert.deepEqual([...routes].sort(), ['/', ...pages].sort(), 'presentation routes cover every app page');
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'suifleet-console-'));
  let server, browser;
  try {
    await fs.access(path.join(output, 'index.html'));
    const fixture = path.join(temporary, 'fixture.js');
    const storageFixture = path.join(temporary, 'storage.js');
    await fs.writeFile(storageFixture, 'export const useFleetBlobStore = () => ({store:null});');
    await build({ stdin: { contents: `import React from 'react'; import {createRoot} from 'react-dom/client';
      import {GreengrassImport} from '@/components/GreengrassImport'; import {HistoryStatusView} from '@/components/HistoryStatus'; import {Section, HealthBadge} from '@/components/ui';
      function App() { const [message, setMessage] = React.useState('History unavailable before checkpoint 123; use an archival endpoint.');
        return <main className="fleet-content"><h1>Console states</h1><button onClick={()=>setMessage('')}>Clear diagnostic</button>
          <Section title="Devices"><table><thead><tr><th>Device</th><th>Health</th></tr></thead><tbody><tr><td>Factory sensor</td><td><HealthBadge health={{status:'healthy',codes:[],reasons:['Signed report verified'],components:[]}} /></td></tr></tbody></table></Section>
          <Section title="Loading"><p role="status">Loading device reports…</p></Section><Section title="Error"><p role="alert">Could not refresh devices. Retry the connection.</p><button disabled>Unavailable action</button></Section>
          <GreengrassImport catalog="" owner="" execute={()=>{throw Error("No signing in presentation fixture");}} onPublished={()=>{}} disabled={true} policies={[]} protect={()=>{throw Error("No uploads in presentation fixture");}}/><HistoryStatusView message={message}/></main>; } createRoot(document.getElementById('root')).render(<App/>);`, resolveDir: root, loader: 'tsx' }, bundle: true, platform: 'browser', outfile: fixture, jsx: 'automatic', nodePaths: [path.join(root, 'dapp/node_modules')], alias: { '@': path.join(root, 'dapp/src'), '@/hooks/useFleetStorage': storageFixture }, define: { 'process.env': '{}', 'process.env.NODE_ENV': '"production"' }, logLevel: 'warning' });
    const css = (await fs.readdir(path.join(output, '_next/static/chunks'))).filter(p=>p.endsWith('.css'));
    server = http.createServer(async (request, response) => {
      try {
        let name = new URL(request.url, 'http://localhost').pathname;
        if (name === '/fixture.js') { response.setHeader('Content-Type','text/javascript'); return response.end(await fs.readFile(fixture)); }
        if (name === '/fixture/') { response.setHeader('Content-Type','text/html'); return response.end(`<!doctype html><html><head>${css.map(p=>`<link rel="stylesheet" href="/_next/static/chunks/${p}">`).join('')}</head><body><div id="root"></div><script src="/fixture.js"></script></body></html>`); }
        if (name.endsWith('/')) name += 'index.html';
        const file = path.resolve(output, '.' + name);
        if (!file.startsWith(output + path.sep)) { response.writeHead(403); return response.end(); }
        response.setHeader('Content-Type', file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.html')?'text/html':'text/plain');
        response.end(await fs.readFile(file));
      } catch { response.writeHead(404); response.end(); }
    });
    await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless:true, args:['--no-sandbox'] });
    const page = await browser.newPage(); const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    // An accidental real network dependency fails rather than spending or using a real account.
    await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
    const base = `http://127.0.0.1:${server.address().port}`;
    for (const width of [1440,390]) {
      await page.setViewportSize({width,height:1000});
      for (const route of routes) {
        await page.goto(base+route,{waitUntil:'networkidle'});
        await page.locator('h1').waitFor();
        assert.doesNotMatch(await page.locator('main').textContent(), /SF-\d{3}|docs\/[A-Z_]+\.md/, `${route}: no internal tracker/document references, even in collapsed details`);
        assert.equal(await page.locator('h1').count(),1,`${route}: one page title`);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${route}: no page overflow at ${width}`);
        const current=page.locator('nav[aria-label="Main"]:visible [aria-current="page"]');
        assert.equal(await current.count(),1,`${route}: current navigation announced`);
        // Signed out, every fleet page names itself and asks for sign-in rather
        // than implying an account or configuration problem.
        if (route !== '/get-started/') {
          const main = page.locator('main');
          await main.getByRole('heading',{name:'Sign in to continue',exact:true}).waitFor();
          assert.equal((await page.locator('h1').textContent()).trim(),(await current.textContent()).trim(),`${route}: prompt titled for the page`);
          assert.ok((await main.locator('header p').textContent()).trim().length>20,`${route}: one-line page description`);
          assert.doesNotMatch(await main.textContent(),/this account|this wallet|fleet configuration|unavailable|not loaded|Connect a wallet|Select or create a fleet/i,`${route}: no account or configuration wording while signed out`);
        } else {
          // Get started is the sign-in flow itself, even when fleets need a selection.
          await page.locator('main').getByRole('heading',{name:'Sign in or connect'}).waitFor();
          assert.doesNotMatch(await page.locator('main').textContent(),/Select or create a fleet/,`${route}: sign-in step is not hidden behind fleet selection`);
        }
      }
      await page.goto(base+'/fixture/',{waitUntil:'networkidle'});
      await page.getByRole('heading',{name:'Console states'}).waitFor();
      // Exercise actual translation results, not just the empty import form.
      const cases = JSON.parse(await fs.readFile(path.join(root, 'interfaces/compat/aws-recipe-cases.json')));
      for (const code of ['unsupported-component-type', 'ipc-access-control', 'aws-managed-dependency', 'privileged-step']) {
        const item = cases.find(c => [...(c.expected.reasons??[]), ...(c.expected.informational??[])].some(r => code === 'aws-managed-dependency' ? r.startsWith('ComponentDependencies.') || r === code : r === code));
        assert.ok(item, `recipe fixture for ${code}`);
        await page.getByRole('textbox', {name:'Recipe (JSON or YAML)'}).fill(item.recipe_text ?? JSON.stringify(item.recipe));
        await page.getByRole('button', {name:'Check translation'}).click();
        const result = page.locator('div[role="status"]').filter({has:page.locator('ul')});
        await result.waitFor();
        const text = await result.textContent();
        assert.ok((code === 'aws-managed-dependency' ? /ComponentDependencies\.|aws-managed-dependency/.test(text) : text.includes(code)), `${code} rendered`);
        assert.doesNotMatch(text, /SF-\d{3}|docs\/[A-Z_]+\.md|adapter ticket|roadmap gap/, `${code}: customer-facing rendered guidance`);
      }
      const disclosure=page.locator('footer > details');
      assert.equal(await disclosure.getAttribute('open'),null);
      assert.equal(await page.getByText('History unavailable before checkpoint 123;', {exact:false}).isVisible(),false);
      const summary=disclosure.locator('summary').first(); await summary.focus(); await page.keyboard.press('Enter');
      assert.equal(await page.getByText('An empty list does not confirm', {exact:false}).isVisible(),true);
      const technical=page.getByText('Technical details for your administrator',{exact:true});
      await technical.focus(); await page.keyboard.press('Enter');
      assert.equal(await page.getByText('History unavailable before checkpoint 123;', {exact:false}).isVisible(),true);
      assert.equal(await page.locator('footer [role="alert"]').count(),0);
      assert.equal(await page.getByRole('button',{name:'Unavailable action'}).isDisabled(),true);
      assert.equal(await page.getByText('Healthy',{exact:true}).isVisible(),true);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.getByRole('button',{name:'Clear diagnostic'}).click(); assert.equal(await page.locator('footer').count(),0);
    }
    assert.deepEqual(errors,[]);
    console.log('PASS: 14 exported routes at desktop/mobile; navigation, page titles and overflow; signed-out sign-in prompt with page description and no account/configuration wording on every fleet route, and the Get started sign-in step; populated/loading/error states; four populated Greengrass rejection guidance cases without internal paths; keyboard history disclosures and empty diagnostics. No external network or wallet.');
  } finally {
    if(browser) await browser.close();
    if(server) await new Promise(resolve=>server.close(resolve));
    await fs.rm(temporary,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
