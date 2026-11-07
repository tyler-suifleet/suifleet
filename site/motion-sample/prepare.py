#!/usr/bin/env python3
"""Prepare local, hash-verified female speech and self-hosted assets for the sample."""
import hashlib,importlib.metadata,json,re,shutil,wave
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def prepare():
 import numpy as np
 from kokoro_onnx import Kokoro
 base=Path(__file__).resolve().parent;script=json.loads((base/'script.json').read_text());config=json.loads((ROOT/'site/guide/narration.json').read_text())
 cache=Path.home()/'.cache/suifleet/narration';model=cache/'kokoro-v1.0.onnx';voices=cache/'voices-v1.0.bin'
 for file,key in [(model,'model_sha256'),(voices,'voices_sha256')]:assert hashlib.sha256(file.read_bytes()).hexdigest()==config[key]
 assert importlib.metadata.version('kokoro-onnx')==config['engine_version']
 engine=Kokoro(model,voices);rate=24000;track=np.zeros(round(script['duration']*rate),dtype=np.float32);cues=[]
 for i,cue in enumerate(script['cues']):
  text=re.sub(r'\bSui\b','swee',re.sub(r'\bSuiFleet\b','Swee Fleet',cue['text']));samples,sr=engine.create(text,voice=script['voice'],speed=1.04,lang='en-us');assert sr==rate and np.isfinite(samples).all()
  end=cue['start']+len(samples)/rate;limit=script['cues'][i+1]['start']-.15 if i+1<len(script['cues']) else script['duration']-.15
  if end>limit:raise ValueError(f'Cue needs more room: {cue["text"]}: {end} > {limit}')
  start=round(cue['start']*rate);track[start:start+len(samples)]=samples;cues.append({'text':cue['text'],'speech_start':cue['start'],'speech_end':end})
 public=base/'public';public.mkdir(exist_ok=True)
 with wave.open(str(public/'narration.wav'),'wb') as out:out.setnchannels(1);out.setsampwidth(2);out.setframerate(rate);out.writeframes((np.clip(track,-1,1)*32767).astype('<i2').tobytes())
 for name in ['inter-variable.woff2','logo.svg']:shutil.copyfile(ROOT/'site/src/assets'/name,public/name)
 shutil.copyfile(base/'art/editorial-scene.png',public/'editorial-scene.png')
 (public/'timing.json').write_text(json.dumps({'duration':script['duration'],'cues':cues},indent=2)+'\n');print('Prepared 20-second female narration, with no overlapping or truncated cues')
if __name__=='__main__':prepare()
