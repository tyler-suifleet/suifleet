import {bundle} from '@remotion/bundler';
import {renderMedia,renderStill,selectComposition} from '@remotion/renderer';
import {availableParallelism} from 'node:os';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.dirname(fileURLToPath(import.meta.url)),out=path.resolve(process.argv[2]);
await fs.mkdir(out,{recursive:true});
const serveUrl=await bundle({entryPoint:path.join(root,'src/overview.tsx'),publicDir:path.join(root,'public')});
const options={serveUrl,browserExecutable:'/usr/bin/google-chrome',chromiumOptions:{disableWebSecurity:false},logLevel:'error'};
for(const id of ['Landscape','Portrait']){
 const composition=await selectComposition({...options,id});
 for(const frame of [75,540,900,1260,1620,1980,2340,2700])await renderStill({...options,composition,frame,output:path.join(out,`overview-${id.toLowerCase()}-${frame}.png`)});
 console.log('Rendered review stills',id);
 if(!process.argv.includes('--stills-only')){
  await renderMedia({...options,composition,codec:'h264',crf:25,pixelFormat:'yuv420p',audioCodec:'aac',audioBitrate:'160k',concurrency:Math.min(2,availableParallelism()),outputLocation:path.join(out,`overview-${id.toLowerCase()}.mp4`)});
  console.log('Rendered real Remotion video',id);
 }
}
