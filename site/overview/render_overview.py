#!/usr/bin/env python3
"""Render original diagram animation and local female narration. No chain/wallet calls."""
import argparse, hashlib, importlib.metadata, json, math, os, re, shutil, subprocess, sys, wave
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'site/guide'))
from encode_guide import image,ffmpeg
from narrate_guide import probe
from captions import webvtt
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--model',type=Path,required=True);p.add_argument('--voices',type=Path,required=True);a=p.parse_args()
 if a.out.exists():raise ValueError('Output must be a new immutable directory')
 a.runtime.mkdir(parents=True,exist_ok=True);config=json.loads((ROOT/'site/guide/narration.json').read_text());story=json.loads((ROOT/'site/overview/storyboard.json').read_text());assert sha(a.model)==config['model_sha256'] and sha(a.voices)==config['voices_sha256'];assert importlib.metadata.version('kokoro-onnx')==config['engine_version']
 import numpy as np
 from kokoro_onnx import Kokoro
 model=Kokoro(a.model,a.voices);fps=20;timeline=[];audio=[];position=0;cues=[]
 for scene in story['scenes']:
  spoken=re.sub(r'\bSuiFleet\b','Swee Fleet',scene['text']);spoken=re.sub(r'\bSui\b','swee',spoken)
  samples,rate=model.create(spoken,voice=story['voice'],speed=1.0,lang=story['language']);assert rate==24000 and len(samples)>0 and np.isfinite(samples).all()
  speech=len(samples)/rate;duration=math.ceil((speech+1.2)*fps)/fps;timeline.append({**scene,'start':position,'duration':duration});cues.append({'text':scene['text'],'speech_start':position+.35,'speech_end':position+.35+speech});audio.extend([np.zeros(round(.35*rate),dtype=np.float32),samples,np.zeros(round((duration-.35-speech)*rate),dtype=np.float32)]);position+=duration
 joined=np.concatenate(audio)
 with wave.open(str(a.runtime/'narration.wav'),'wb') as wav:wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes((np.clip(joined,-1,1)*32767).astype('<i2').tobytes())
 (a.runtime/'timeline.json').write_text(json.dumps({'fps':fps,'duration':position,'scenes':timeline},indent=2)+'\n');print('Narration ready',round(position,2),'seconds',flush=True)
 subprocess.run(['node',str(ROOT/'site/overview/render_frames.cjs'),str(a.runtime)],check=True,cwd=ROOT)
 a.out.mkdir(parents=True);tag=image();manifest={'schema':1,'kind':'illustrative-animation','title':story['title'],'duration_seconds':round(position,2),'voice':story['voice'],'language':story['language'],'synthetic_voice':True,'scenes':timeline,'cues':cues,'source_sha256':{str(p.relative_to(ROOT)):sha(p) for p in [ROOT/'site/overview/storyboard.json',ROOT/'site/overview/scene.html',ROOT/'site/overview/render_frames.cjs',Path(__file__)]},'variants':[]}
 for name,width,height in [('landscape',1280,720),('portrait',720,1280)]:
  work=a.runtime/name;shutil.copyfile(a.runtime/'narration.wav',work/'narration.wav');frames=round(position*fps)
  ffmpeg(tag,work,'-framerate',str(fps),'-i','%05d.png','-i','narration.wav','-map','0:v:0','-map','1:a:0','-c:v','libx264','-crf','24','-preset','medium','-pix_fmt','yuv420p','-c:a','aac','-b:a','128k','-movflags','+faststart','-t',str(position),'-map_metadata','-1','overview.mp4')
  ffmpeg(tag,work,'-i','overview.mp4','-c:v','libvpx-vp9','-crf','34','-b:v','0','-row-mt','1','-cpu-used','4','-c:a','libopus','-b:a','96k','-map_metadata','-1','overview.webm')
  ffmpeg(tag,work,'-ss',str(timeline[-1]['start']+1),'-i','overview.mp4','-frames:v','1','-q:v','3','poster.jpg')
  v={'name':name,'width':width,'height':height}
  for source,key,suffix in [('overview.mp4','fallback','mp4'),('overview.webm','file','webm'),('poster.jpg','poster','jpg')]:
   dest=a.out/f'overview-{name}.{suffix}';shutil.copyfile(work/source,dest);assert dest.stat().st_size<15_000_000;v[key]=dest.name;v[key+'_sha256']=sha(dest)
  facts=probe(tag,work,'overview.mp4');assert len([s for s in facts['streams'] if s['codec_type']=='audio'])==1;assert abs(float(facts['format']['duration'])-position)<.3
  for source in ['overview.mp4','overview.webm']:ffmpeg(tag,work,'-v','error','-xerror','-i',source,'-f','null','-')
  manifest['variants'].append(v);print('Encoded and fully decoded',name,flush=True)
 track=a.out/'overview.en.vtt';track.write_text(webvtt(cues,position));manifest.update(captions=track.name,captions_sha256=sha(track));(a.out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
if __name__=='__main__':main()
