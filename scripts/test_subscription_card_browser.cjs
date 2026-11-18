const assert = require('node:assert/strict'), fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const { appRequire } = require('./frontend_test_utils.cjs');
const { build } = appRequire('esbuild'), { chromium } = appRequire('playwright');
(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sf183-browser-'));
  let browser;
  const root = path.resolve('dapp'), addr = n => '0x' + n.toString(16).padStart(64, '0');
  const binding = { chain: '4c78adac', package: addr(4), wallet: addr(1), customer: addr(2), coinType: addr(5)+'::usdc::USDC', quote: { offerId: addr(3), symbol: 'USDC', source: 'browser test', observedAt: Date.now()-1000, usdPerCoin: 1 }, total: '49000000', expiresAt: Date.now()+86400000 };
  try {
    await build({ stdin: { contents: `import React from 'react'; import {createRoot} from 'react-dom/client'; import {QueryClient,QueryClientProvider} from '@tanstack/react-query'; import {SubscriptionCard} from './src/components/SubscriptionCard';
window.funds={usdc:0n,sui:10000000n}; window.qc=new QueryClient({defaultOptions:{queries:{retry:false}}}); window.calls=0;
const binding=${JSON.stringify(binding)}; window.binding=binding;
createRoot(document.getElementById('root')).render(<QueryClientProvider client={window.qc}><SubscriptionCard client={{}} quote={binding.quote} wallet={binding.wallet} customer={binding.customer} disabled={false} prepay={async submitted=>{window.calls++;submitted('browser-effects-pending');await new Promise(r=>window.finish=r);return true;}} /></QueryClientProvider>);`, loader: 'tsx', resolveDir: root }, bundle: true, platform: 'browser', outfile: path.join(tmp, 'app.js'), define: { 'process.env.NODE_ENV': '"production"', 'process.env': '{}'  }, plugins: [{ name: 'chain-fixture', setup(b) {
      b.onResolve({ filter: /^@\/lib\/subscription-card$/ }, () => ({ path: 'fixture', namespace: 'fixture' }));
      b.onLoad({ filter: /.*/, namespace: 'fixture' }, () => ({ contents: `export {CARD_DISABLED,bindingKey,cardStatus,loadCardPurchase,saveCardPurchase,startCardPurchase} from '${root}/src/lib/subscription-card.ts'; export const cardTestMode=()=>true; export const cardBinding=async()=>window.binding; export const cardBalances=async()=>{if(window.fail)throw Error('offline');return window.funds;};`, resolveDir: root }));
      b.onResolve({ filter: /^@\/lib\/subscription$/ }, () => ({ path: 'coin', namespace: 'coin' }));
      b.onLoad({ filter: /.*/, namespace: 'coin' }, () => ({ contents: 'export const coin=()=>({decimals:6});' }));
      b.onResolve({ filter: /^@\// }, args => ({ path: path.join(root, 'src', args.path.slice(2)+'.ts') }));
    }}] });
    fs.writeFileSync(path.join(tmp, 'index.html'), '<div id="root"></div><script src="app.js"></script>');
    browser = await chromium.launch({ headless: true, ...(process.env.CHROME_EXECUTABLE ? { executablePath: process.env.CHROME_EXECUTABLE } : {}) });
    const page = await browser.newPage();
    page.on('pageerror', e=>console.error('browser error:',e.message));
    await page.goto('file://'+path.join(tmp, 'index.html'));
    const button = text => page.getByRole('button', { name: text, exact: true });
    const state = text => page.getByRole('status').filter({ hasText: 'Purchase: '+text }).waitFor();
    async function funds(usdc, sui = '10000000') { await page.evaluate(({usdc,sui}) => { window.funds={usdc:BigInt(usdc),sui:BigInt(sui)};return window.qc.invalidateQueries({queryKey:['card-balances']}); }, {usdc,sui}); }
    await button('Start testnet simulated purchase').click(); await state('waiting');
    await funds('1000000'); await state('partial');
    await button('Abandon checkout; keep wallet funds').click(); await state('abandoned');
    await funds('49000000'); await state('abandoned'); assert.equal(await page.evaluate(()=>window.calls),0);
    await page.reload(); await state('abandoned'); // actual localStorage restore
    await button('Resume tracking and review').click(); await funds('49000000','0'); await state('gas-needed');
    assert.equal(await button('Review and approve subscription prepay').isDisabled(),true);
    await funds('49000000'); await state('ready');
    await button('Review and approve subscription prepay').click(); await state('submitted');
    assert.equal(await page.evaluate(()=>window.calls),1);
    await page.reload(); await state('submitted');
    assert.equal(await button('Review and approve subscription prepay').count(),0); // uncertain reload cannot replay
    await page.evaluate(()=>{localStorage.clear();}); await page.reload();
    await button('Start testnet simulated purchase').click(); await funds('49000000'); await state('ready');
    await button('Review and approve subscription prepay').click(); await state('submitted');
    await page.evaluate(()=>window.finish()); await state('confirmed');
    console.log('PASS: real browser partial/abandoned restore, gas gating, explicit prepay, submitted reload/no replay and effects-confirmed state; chain I/O and wallet are fixtures');
  } finally { if (browser) await browser.close(); fs.rmSync(tmp,{recursive:true,force:true}); }
})().catch(e=>{console.error(e);process.exitCode=1;});
