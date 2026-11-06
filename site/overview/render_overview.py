#!/usr/bin/env python3
"""Render the selected editorial overview with local female narration; no network mutations."""
import argparse,hashlib,importlib.metadata,json,os,re,shutil,subprocess,sys,wave
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'site/guide'))
from encode_guide import image,ffmpeg
from narrate_guide import probe
from captions import webvtt
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
SOURCES=['site/overview/storyboard.json','site/overview/render_overview.py','site/motion-sample/src/overview.tsx','site/motion-sample/overview-render.mjs','site/motion-sample/package.json','site/motion-sample/package-lock.json']
ASSETS=['site/motion-sample/art/editorial-scene.png','site/src/assets/inter-variable.woff2','site/src/assets/logo.svg']
def main():
 p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--model',type=Path,required=True);p.add_argument('--voices',type=Path,required=True);p.add_argument('--prepare-only',action='store_true');p.add_argument('--reuse-render',action='store_true');a=p.parse_args()
 if a.out.exists():raise ValueError('Output must be a new immutable directory')
 a.runtime.mkdir(parents=True,exist_ok=True);config=json.loads((ROOT/'site/guide/narration.json').read_text());story=json.loads((ROOT/'site/overview/storyboard.json').read_text());assert sha(a.model)==config['model_sha256'] and sha(a.voices)==config['voices_sha256'];assert importlib.metadata.version('kokoro-onnx')==config['engine_version']
 import numpy as np
 from kokoro_onnx import Kokoro
 model=Kokoro(a.model,a.voices);fps=30;timeline=[];position=0;cues=[];audio=[]
 for scene in story['scenes']:
  spoken=re.sub(r'\bSui\b','swee',re.sub(r'\bSuiFleet\b','Swee Fleet',scene['text']))
  samples,rate=model.create(spoken,voice=story['voice'],speed=1.04,lang=story['language']);assert rate==24000 and len(samples)>0 and np.isfinite(samples).all()
  speech=len(samples)/rate;duration=scene['duration'];assert duration==12
  if speech>duration-.8:raise ValueError('Speech needs more room: '+scene['id'])
  timeline.append({**scene,'start':position});cues.append({'text':scene['text'],'speech_start':position+.35,'speech_end':position+.35+speech});audio.extend([np.zeros(round(.35*rate),dtype=np.float32),samples,np.zeros(round((duration-.35-speech)*rate),dtype=np.float32)]);position+=duration
 public=ROOT/'site/motion-sample/public';public.mkdir(exist_ok=True);joined=np.concatenate(audio)
 with wave.open(str(public/'overview-narration.wav'),'wb') as wav:wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes((np.clip(joined,-1,1)*32767).astype('<i2').tobytes())
 for source in ASSETS:shutil.copyfile(ROOT/source,public/Path(source).name)
 provenance={'source_sha256':{s:sha(ROOT/s) for s in SOURCES},'assets_sha256':{s:sha(ROOT/s) for s in ASSETS},'narration_sha256':sha(public/'overview-narration.wav')}
 if a.reuse_render:
  if json.loads((a.runtime/'input-receipt.json').read_text())!=provenance:raise ValueError('Stale render inputs')
 else:(a.runtime/'input-receipt.json').write_text(json.dumps(provenance,indent=2)+'\n')
 (a.runtime/'timing.json').write_text(json.dumps(cues,indent=2)+'\n');print('Narration ready:',position,'seconds, no truncation or overlap',flush=True)
 if a.prepare_only:return
 if not a.reuse_render:subprocess.run(['node',str(ROOT/'site/motion-sample/overview-render.mjs'),str(a.runtime)],check=True,cwd=ROOT)
 a.out.mkdir(parents=True);tag=image();manifest={'schema':1,'kind':'illustrative-animation','renderer':'Remotion','renderer_version':'4.0.541','title':story['title'],'duration_seconds':position,'fps':fps,'voice':story['voice'],'language':story['language'],'synthetic_voice':True,'scenes':timeline,'cues':cues,**provenance,'variants':[]}
 for name,width,height in [('landscape',1280,720),('portrait',720,1280)]:
  source=a.runtime/f'overview-{name}.mp4';facts=probe(tag,a.runtime,source.name);video=next(s for s in facts['streams'] if s['codec_type']=='video');assert (video['width'],video['height'])==(width,height);assert video['r_frame_rate']=='30/1';assert any(s['codec_type']=='audio' for s in facts['streams']);assert abs(float(facts['format']['duration'])-position)<.2
  ffmpeg(tag,a.runtime,'-i',source.name,'-c:v','libvpx-vp9','-crf','36','-b:v','0','-row-mt','1','-cpu-used','5','-c:a','libopus','-b:a','96k','-map_metadata','-1',f'overview-{name}.webm')
  v={'name':name,'width':width,'height':height}
  for filename,key in [(f'overview-{name}.mp4','fallback'),(f'overview-{name}.webm','file')]:
   ffmpeg(tag,a.runtime,'-v','error','-xerror','-i',filename,'-f','null','-');dest=a.out/filename;shutil.copyfile(a.runtime/filename,dest);assert dest.stat().st_size<25*1024*1024;v[key]=filename;v[key+'_sha256']=sha(dest)
  ffmpeg(tag,a.runtime,'-i',f'overview-{name}-75.png','-frames:v','1','-q:v','2',f'overview-{name}.jpg');poster=a.out/f'overview-{name}.jpg';shutil.copyfile(a.runtime/poster.name,poster);v.update(poster=poster.name,poster_sha256=sha(poster));manifest['variants'].append(v);print('Encoded and fully decoded',name,flush=True)
 track=a.out/'overview.en.vtt';track.write_text(webvtt(cues,position));manifest.update(captions=track.name,captions_sha256=sha(track));(a.out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
if __name__=='__main__':main()
