// Exercises the exported console with a disposable browser and local-only server.
// No wallet, network fixtures, chain submissions or user's browser profile.
const fs = require('node:fs/promises'), path = require('node:path'), os = require('node:os'), http = require('node:http');
const assert = require('node:assert/strict');
const { appRequire } = require('./frontend_test_utils.cjs');
const { build } = appRequire('esbuild'), { chromium } = appRequire('playwright');
const root = path.resolve(__dirname, '..'), output = path.join(root, 'dapp/out');
// Engineering references never belong in customer copy: configuration variable
// names, internal document paths and tracker IDs.
const INTERNAL = /NEXT_PUBLIC_|docs\/|SF-\d{3}/;
// All rendered copy, including collapsed disclosures; not script or style source.
const renderedText = page => page.evaluate(() => { const copy = document.body.cloneNode(true);
  copy.querySelectorAll('script,style,template').forEach(e => e.remove()); return copy.textContent; });
const renderedAttributes = page => page.evaluate(() => [...document.body.querySelectorAll('*')]
  .flatMap(e => ['title', 'placeholder', 'aria-label', 'alt'].map(a => e.getAttribute(a) ?? '')).join('\n'));
// Discover pages so new routes automatically inherit the console regression gate.
const routes = require('node:fs').readdirSync(path.join(root, 'dapp/src/app'), {recursive:true})
  .filter(name => /(^|\/)page\.tsx$/.test(name))
  .map(name => '/' + name.replace(/page\.tsx$/, ''));
(async () => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'suifleet-console-'));
  let server, browser;
  try {
    await fs.access(path.join(output, 'index.html'));
    const fixture = path.join(temporary, 'fixture.js');
    const storageFixture = path.join(temporary, 'storage.js');
    // Presentation-only fixture: translation runs locally; storage never becomes ready to upload.
    await fs.writeFile(storageFixture, `
      export const useFleetBlobStore = () => ({store:null});
      export const useFleetStorage = () => ({target:null});
      export const useReleasePeriod = () => ({days:5, setDays:()=>{}, epochs:0,
        loading:true, prepaid:{store:null}, fleet:{},
        publisherStore:()=>{throw Error("No uploads in presentation fixture");},
        fleetStore:()=>{throw Error("No uploads in presentation fixture");}});
    `);
    await build({ stdin: { contents: `import React from 'react'; import {createRoot} from 'react-dom/client';
      import {GreengrassImport} from '@/components/GreengrassImport'; import {HistoryStatusView} from '@/components/HistoryStatus'; import {Section, HealthBadge} from '@/components/ui'; import {ControlEditor} from '@/components/DeploymentControlEditor';
      function App() { const [message, setMessage] = React.useState('History unavailable before checkpoint 123; use an archival endpoint.');
        return <main className="fleet-content"><h1>Console states</h1><button onClick={()=>setMessage('')}>Clear diagnostic</button>
          <Section title="Devices"><table><thead><tr><th>Device</th><th>Health</th></tr></thead><tbody><tr><td>Factory sensor</td><td><HealthBadge health={{status:'healthy',codes:[],reasons:['Signed report verified'],components:[]}} /></td></tr></tbody></table></Section>
          <Section title="Loading"><p role="status">Loading device reports…</p></Section><Section title="Error"><p role="alert">Could not refresh devices. Retry the connection.</p><button disabled>Unavailable action</button></Section>
          <GreengrassImport catalog="" owner="" execute={()=>{throw Error("No signing in presentation fixture");}} onPublished={()=>{}} disabled={true} policies={[]} protect={()=>{throw Error("No uploads in presentation fixture");}}/><HistoryStatusView message={message}/>
          <Section title="Schedule"><ControlEditor control={{revision:'1',state:0,not_before_ms:'1793318400000',expires_ms:'0',window_start_ms:'0',window_end_ms:'0',notice_ms:'0'}} canEdit={true} pending={false} onSave={()=>{}}/></Section></main>; } createRoot(document.getElementById('root')).render(<App/>);`, resolveDir: root, loader: 'tsx' }, bundle: true, platform: 'browser', outfile: fixture, jsx: 'automatic', nodePaths: [path.join(root, 'dapp/node_modules')], alias: { '@': path.join(root, 'dapp/src'), '@/hooks/useFleetStorage': storageFixture, '@/hooks/useReleasePeriod': storageFixture }, define: { 'process.env': '{}', 'process.env.NODE_ENV': '"production"' }, logLevel: 'warning' });
    const css = (await fs.readdir(path.join(output, '_next/static/chunks'))).filter(p=>p.endsWith('.css'));
    server = http.createServer(async (request, response) => {
      try {
        let name = new URL(request.url, 'http://localhost').pathname;
        if (name === '/fixture.js') { response.setHeader('Content-Type','text/javascript'); return response.end(await fs.readFile(fixture)); }
        if (name === '/fixture/') { response.setHeader('Content-Type','text/html'); return response.end(`<!doctype html><html><head>${css.map(p=>`<link rel="stylesheet" href="/_next/static/chunks/${p}">`).join('')}</head><body><div id="root"></div><script src="/fixture.js"></script></body></html>`); }
        if (name.endsWith('/')) name += 'index.html';
        const file = path.resolve(output, '.' + name);
        if (!file.startsWith(output + path.sep)) { response.writeHead(403); return response.end(); }
        response.setHeader('Content-Type', file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.html')?'text/html':file.endsWith('.json')?'application/json':file.endsWith('.ico')?'image/x-icon':'text/plain');
        response.end(await fs.readFile(file));
      } catch { response.writeHead(404); response.end(); }
    });
    await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless:true, args:['--no-sandbox'] });
    const page = await browser.newPage(); const errors=[];
    page.on('pageerror',error=>errors.push(`${page.url()}: ${error.message}`));
    page.on('console',message=>{if(message.type()==='error') errors.push(`${page.url()}: ${message.text()}`);});
    const externalRequests=[];
    const isQuoteRead = url => url.origin === 'https://fullnode.testnet.sui.io' && url.pathname.startsWith('/sui.rpc.v2.');
    page.on('request',request=>{const url=new URL(request.url()); if(url.hostname!=='127.0.0.1' && !isQuoteRead(url)) externalRequests.push(request.url());});
    // An accidental real network dependency fails rather than spending or using a real account.
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.hostname === '127.0.0.1') return route.continue();
      // The disconnected budget estimator reads public testnet prices on mount.
      // Exercise its unavailable-price state without inducing browser network errors.
      if (isQuoteRead(url)) return route.fulfill({status:200, contentType:'application/grpc-web+proto', headers:{'grpc-status':'14','grpc-message':'Offline price fixture'}, body:''});
      return route.abort();
    });
    const base = `http://127.0.0.1:${server.address().port}`;
    for (const width of [1440,390]) {
      await page.setViewportSize({width,height:1000});
      for (const route of routes) {
        await page.goto(base+route,{waitUntil:'networkidle'});
        await page.locator('h1').waitFor().catch(error => {
          throw new Error(`${route} at ${width}px: page title unavailable; console/page errors: ${JSON.stringify(errors)}; external requests: ${JSON.stringify(externalRequests)}`, {cause:error});
        });
        assert.equal(await page.locator('link[rel="icon"]').getAttribute('href'), '/favicon.ico');
        const favicon = await page.request.get(base+'/favicon.ico');
        assert.equal(favicon.status(),200,`${route}: exported favicon`);
        assert.equal((await favicon.body()).readUInt16LE(2),1,'ICO signature');
        const metadata = await page.request.get(base+'/wallet/slush.json');
        assert.equal(metadata.status(),200);
        assert.equal((await metadata.json()).walletName,'Slush');
        assert.deepEqual(errors,[],`${route} at ${width}px: no console/page errors`);
        assert.doesNotMatch(await renderedText(page), INTERNAL, `${route}: no internal tracker/document references or configuration names, even in collapsed details`);
        assert.doesNotMatch(await renderedAttributes(page), INTERNAL, `${route}: no internal references in labels, titles or placeholders`);
        assert.equal(await page.locator('h1').count(),1,`${route}: one page title`);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${route}: no page overflow at ${width}`);
        const current=page.locator('nav[aria-label="Main"]:visible [aria-current="page"]');
        assert.equal(await current.count(),1,`${route}: current navigation announced`);
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
      assert.doesNotMatch(await renderedText(page), INTERNAL, 'fixture: no internal references');
      assert.doesNotMatch(await renderedAttributes(page), INTERNAL, 'fixture: no internal references in attributes');
      // Schedule fields are named in words with their timezone; no units beside date pickers.
      await page.getByRole('button',{name:'Edit schedule / notice'}).click();
      for (const name of ['Not before (UTC)', 'Expires (UTC)', 'Window start (UTC)', 'Window end (UTC)']) {
        const field = page.getByLabel(name, {exact:true});
        assert.equal(await field.getAttribute('type'), 'datetime-local', `${name}: date picker`);
        assert.match(await field.getAttribute('placeholder'), /^\d{4}-\d{2}-\d{2}T/, `${name}: example placeholder`);
      }
      assert.match(await page.getByLabel('Not before (UTC)', {exact:true}).inputValue(), /^2026-10-30T00:00/);
      const dateLabels = await page.locator('label:has(input[type="datetime-local"])').allTextContents();
      assert.equal(dateLabels.length, 4);
      for (const text of dateLabels) assert.doesNotMatch(text, /\bms\b|_|millisecond/i, `date label ${text}`);
      await page.getByRole('button',{name:'Edit schedule / notice'}).click();
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
    assert.deepEqual(externalRequests,[],'no external metadata or other startup requests');
    console.log(`PASS: ${routes.length} exported routes at desktop/mobile; navigation, page titles and overflow; populated/loading/error states; four populated Greengrass rejection guidance cases without internal paths; keyboard history disclosures and empty diagnostics. No console errors, external network or wallet; testnet quote reads use an unavailable-price fixture.`);
  } finally {
    if(browser) await browser.close();
    if(server) await new Promise(resolve=>server.close(resolve));
    await fs.rm(temporary,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
