// Real React Query hook and quote controls; deterministic read failures, no wallet/network writes.
const fs=require('node:fs/promises'), path=require('node:path'),os=require('node:os'),http=require('node:http'),assert=require('node:assert/strict');
const {appRequire}=require('./frontend_test_utils.cjs');
const {build}=appRequire('esbuild'),{chromium}=appRequire('playwright');
(async()=>{const root=path.resolve(__dirname,'..'),tmp=await fs.mkdtemp(path.join(os.tmpdir(),'release-quote-'));let server,browser;
try {
 const mocks=path.join(tmp,'reads.ts');
 await fs.writeFile(mocks,`import {useQuery} from '@tanstack/react-query';
 export const NETWORK='testnet',WALRUS_EPOCHS=5;
 export const useCurrentClient=()=>({}); export const useCurrentAccount=()=>({address:'0x1'});
 export const useFleetStorage=()=>{const q=useQuery({queryKey:['fleet-budgets',NETWORK],queryFn:async()=>({id:'0xb'})});return {budget:q.data,loading:q.isLoading};};
 export const useFleetBlobStore=()=>({}); export const useFleetTransaction=()=>({mutateAsync:()=>{window.writes++;throw Error('Unexpected transaction');}});
 export async function fetchFleetSettings(){window.reads++;await new Promise(r=>setTimeout(r,150));if(!window.recovered)throw Error('Transient unavailable');return undefined;}
 export const DAY=86400000;
 export const storageSchedule=async()=>({epoch:572,epochDurationMs:DAY,firstEpochStartMs:1743692414626,maxEpochs:52});
 export const periodEpochs=(days)=>Math.ceil(days); export const expiryTime=()=>undefined;
 export const releaseWalrus=async()=>({storageCost:async()=>({totalCost:1n})});
 export const buildExtendFleetReleaseTx=()=>{throw Error('Unexpected extension')};export const requireFleetUploadPeriod=()=>{};
 export const uploadBlob=()=>{window.writes++;throw Error('Unexpected upload');};`);
 // Preserve the real remaining-term calculation while isolating remote read transports.
 const actual=await fs.readFile(path.join(root,'dapp/src/lib/release-storage.ts'),'utf8');
 const calculation=actual.slice(actual.indexOf('export function storageRemaining'),actual.indexOf('export async function releaseWalrus'));
 await fs.appendFile(mocks,'\n'+calculation);
 const bundle=path.join(tmp,'app.js');
 await build({stdin:{resolveDir:root,loader:'tsx',contents:`import React from 'react';import {createRoot} from 'react-dom/client';import {QueryClient,QueryClientProvider} from '@tanstack/react-query';import {useReleasePeriod} from '@/hooks/useReleasePeriod';import {ReleasePeriod} from '@/components/ReleasePeriod';
 window.reads=0;window.writes=0;window.recovered=false;
 const client=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
 function App(){const [file,setFile]=React.useState();const period=useReleasePeriod(file?[file.size]:[]);return <main><input aria-label="Artifact" type="file" onChange={e=>setFile(e.target.files[0])}/><p>{file?.name}</p><button onClick={()=>window.recovered=true}>Restore service</button><ReleasePeriod period={period}/><output>{period.error?'Unavailable':period.loading?'Checking':'Ready'}</output></main>};createRoot(document.getElementById('root')).render(<QueryClientProvider client={client}><App/></QueryClientProvider>);`},plugins:[{name:'isolated-hook-dependencies',setup(b){b.onResolve({filter:/^\.\/useFleet(Storage|Transaction)$/},()=>({path:mocks}));}}],bundle:true,platform:'browser',outfile:bundle,jsx:'automatic',nodePaths:[path.join(root,'dapp/node_modules')],alias:{'@':path.join(root,'dapp/src'),'@mysten/dapp-kit-react':mocks,'@/hooks/useCurrentAccount':mocks,'@/lib/fleet-settings':mocks,'@/lib/constants':mocks,'@/lib/release-storage':mocks,'@/lib/walrus':mocks},define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});
 server=http.createServer(async(req,res)=>{res.setHeader('Content-Type',req.url==='/app.js'?'text/javascript':'text/html');res.end(req.url==='/app.js'?await fs.readFile(bundle):'<div id="root"></div><script src="/app.js"></script>');});await new Promise(r=>server.listen(0,'127.0.0.1',r));
 browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE,headless:true,args:['--no-sandbox']});
 for(const width of [390,1440]){const page=await browser.newPage({viewport:{width,height:844}}),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(`http://127.0.0.1:${server.address().port}`);await page.getByRole('alert').waitFor();
 await page.getByLabel('Artifact',{exact:true}).setInputFiles({name:'customer-update.bin',mimeType:'application/octet-stream',buffer:Buffer.from('customer artifact')});await page.getByLabel('Storage period (days)').fill('8');
 await page.getByRole('button',{name:'Refresh storage quote',exact:true}).click();await page.locator('p[role="status"]').waitFor();await page.getByRole('button',{name:'Refresh storage quote',exact:true}).waitFor();assert.equal(await page.getByRole('alert').count(),1,'Persistent failure stays visible');
 await page.getByRole('button',{name:'Restore service'}).click();await page.getByRole('button',{name:'Refresh storage quote',exact:true}).click();await page.locator('p[role="status"]').waitFor();await page.waitForFunction(()=>document.querySelector('output').textContent==='Ready');
 assert.equal(await page.getByRole('alert').count(),0);assert.equal(await page.getByLabel('Storage period (days)').inputValue(),'8');assert.equal(await page.getByLabel('Artifact',{exact:true}).evaluate(el=>el.files[0].name),'customer-update.bin');assert.match(await page.locator('main').textContent(),/7–8 days/);assert.doesNotMatch(await page.locator('main').textContent(),/Projected expiry|Nov 1/);assert.equal(await page.evaluate(()=>window.writes),0);assert.ok(await page.evaluate(()=>window.reads)>=3);assert.deepEqual(errors,[]);await page.close();}
 console.log('quote recovery: real hook, persistent failure, successful refresh, retained file/period, mobile/desktop, no writes passed');
}finally{await browser?.close();await new Promise(r=>server?server.close(r):r());await fs.rm(tmp,{recursive:true,force:true});}})().catch(e=>{console.error(e);process.exitCode=1;});
