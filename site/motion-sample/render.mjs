import {bundle} from '@remotion/bundler';
import {renderMedia,renderStill,selectComposition} from '@remotion/renderer';
import {createHash} from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.dirname(fileURLToPath(import.meta.url)),out=path.resolve(process.argv[2]);
await fs.mkdir(out,{recursive:true});
const serveUrl=await bundle({entryPoint:path.join(root,'src/index.tsx'),publicDir:path.join(root,'public')});
const options={serveUrl,browserExecutable:'/usr/bin/google-chrome',chromiumOptions:{disableWebSecurity:false},logLevel:'error'};
for(const id of ['Landscape','Portrait']){
 const composition=await selectComposition({...options,id});
 for(const frame of [75,210,330,495,585])await renderStill({...options,composition,frame,output:path.join(out,`${id.toLowerCase()}-${frame}.png`)});
 console.log('Rendered review stills',id);
 if(!process.argv.includes('--stills-only')){
  await renderMedia({...options,composition,codec:'h264',crf:18,pixelFormat:'yuv420p',audioCodec:'aac',audioBitrate:'160k',concurrency:3,outputLocation:path.join(out,`${id.toLowerCase()}.mp4`)});
  console.log('Rendered real Remotion video',id);
 }
}
if(!process.argv.includes('--stills-only')){
 const digest=async p=>createHash('sha256').update(await fs.readFile(p)).digest('hex');
 const sourceFiles=['src/index.tsx','render.mjs','prepare.py','script.json','package.json','package-lock.json'];
 const source_sha256=Object.fromEntries(await Promise.all(sourceFiles.map(async p=>[`site/motion-sample/${p}`,await digest(path.join(root,p))])));
 const assets_sha256=Object.fromEntries(await Promise.all(['inter-variable.woff2','logo.svg'].map(async name=>[`site/src/assets/${name}`,await digest(path.join(root,'public',name))])));
 const timing=JSON.parse(await fs.readFile(path.join(root,'public/timing.json')));
 await fs.writeFile(path.join(out,'render-receipt.json'),JSON.stringify({renderer:'Remotion',version:'4.0.541',fps:30,duration_seconds:20,source_sha256,assets_sha256,narration_sha256:await digest(path.join(root,'public/narration.wav')),timing,videos:await Promise.all(['landscape','portrait'].map(async name=>({name,file:`${name}.mp4`,sha256:await digest(path.join(out,`${name}.mp4`))})))},null,2)+'\n');
}
