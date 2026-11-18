#!/usr/bin/env python3
"""Produce purpose-led lessons from verified historical footage and local female speech."""
import argparse,copy,hashlib,importlib.metadata,json,math,re,shutil,subprocess,sys,wave
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(Path(__file__).resolve().parent))
from encode_guide import image,ffmpeg
from narrate_guide import probe
from captions import webvtt
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
SOURCES=['site/guide/render_lessons.py','site/guide/lesson_cards.cjs','site/guide/lessons.json','site/guide/narration.json','site/guide/captions.py','site/guide/encode_guide.py','site/guide/narrate_guide.py','scripts/video-test.Dockerfile']
ASSETS=['site/src/assets/logo.svg','site/src/assets/inter-variable.woff2','site/motion-sample/art/editorial-scene.png']
FPS=25;FADE=.32

def main():
 p=argparse.ArgumentParser();p.add_argument('--source-media',type=Path,required=True);p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--steps');p.add_argument('--reuse',action='store_true');a=p.parse_args()
 if a.output.exists() or a.output.resolve()==a.source_media.resolve():raise ValueError('Use a new output directory outside the input')
 config=json.loads((ROOT/'site/guide/lessons.json').read_text());voice=json.loads((ROOT/'site/guide/narration.json').read_text());source=json.loads((a.source_media/'videos.json').read_text())
 if source.get('tutorial_production'):raise ValueError('Use the reviewed unproduced source clips, not already edited lessons')
 clips={v['step']:v for v in source['videos']};selected=set(a.steps.split(',')) if a.steps else set(clips)
 if not selected<=set(clips) or {l['step'] for l in config['lessons']}!=set(clips):raise ValueError('Lesson/recording coverage differs')
 for l in config['lessons']:
  v=clips[l['step']]
  if len(l['actions'])!=len(v['narration']['cues']) or l['source_sha256']!=v['file_sha256'] or sha(a.source_media/v['file'])!=l['source_sha256']:raise ValueError('Unreviewed source or cue count: '+l['step'])
 cache=Path.home()/'.cache/suifleet/narration';model=cache/'kokoro-v1.0.onnx';voices=cache/'voices-v1.0.bin'
 assert sha(model)==voice['model_sha256'] and sha(voices)==voice['voices_sha256']
 assert importlib.metadata.version('kokoro-onnx')==voice['engine_version']
 import numpy as np
 from kokoro_onnx import Kokoro
 engine=Kokoro(str(model),str(voices));tag=image();a.runtime.mkdir(parents=True,exist_ok=True);a.output.mkdir()
 provenance={'source_sha256':{n:sha(ROOT/n) for n in SOURCES},'assets_sha256':{n:sha(ROOT/n) for n in ASSETS},'source_manifest_sha256':sha(a.source_media/'videos.json')}
 # Cards are code-native graphics. Render once after checking the input, with no external requests.
 subprocess.run(['node',str(ROOT/'site/guide/lesson_cards.cjs'),str(a.runtime)],check=True,cwd=ROOT)
 result=copy.deepcopy(source);result['source_postprocessing']={'caption_strip_removal':result.pop('caption_strip_removal',None)};result['videos']=[];result['tutorial_production']={**provenance,'note':'Edited historical app footage with branded graphics and new narration; waits may be sped up. Not fresh live acceptance.'}
 result['narration']={k:voice[k] for k in ['engine','engine_version','voice','voice_description','model_sha256','voices_sha256']};result['narration']['script_sha256']=sha(ROOT/'site/guide/lessons.json')
 for lesson in config['lessons']:
  key=lesson['step']
  if key not in selected:continue
  original=clips[key];work=a.runtime/key;receipt=work/'completed.json';identity={**provenance,'step':key}
  if a.reuse and receipt.exists():
   prior=json.loads(receipt.read_text())
   if prior['inputs']==identity and all(sha(work/'produced'/n)==h for n,h in prior['files'].items()):
    for n in prior['files']:shutil.copyfile(work/'produced'/n,a.output/n)
    result['videos'].append(prior['video']);print('Reused verified lesson:',key,flush=True);continue
  shutil.copyfile(a.source_media/original['file'],work/'source.webm');plan=[];position=0
  parts=[('intro',lesson['intro'],None),*[(f'action-{i}',action['text'],cue) for i,(action,cue) in enumerate(zip(lesson['actions'],original['narration']['cues']))],('outro',lesson['outro'],None)]
  for i,(kind,text,cue) in enumerate(parts):
   spoken=re.sub(r'\bSui\b','swee',re.sub(r'\bSuiFleet\b','Swee Fleet',text))
   samples,rate=engine.create(spoken,voice=config['voice'],speed=1.04,lang='en-us');assert rate==24000 and len(samples)>0 and np.isfinite(samples).all()
   duration=math.ceil(max(4,len(samples)/rate+.9)*FPS)/FPS
   if kind.startswith('action-') and lesson['actions'][i-1].get('wait'):duration=max(duration,8)
   start=position if i==0 else position-FADE
   segment={'kind':kind,'text':text,'start':start,'duration':duration,'speech_start':start+.45,'speech_end':start+.45+len(samples)/rate}
   assert segment['speech_end']<start+duration-.3
   if cue:segment.update(source_start=cue['start'],source_end=cue['start']+cue['duration'])
   plan.append((segment,samples));position=start+duration
  track=np.zeros(round(position*24000),dtype=np.float32)
  for seg,samples in plan:
   start=round(seg['speech_start']*24000);track[start:start+len(samples)]=samples
  peak=float(np.max(np.abs(track)));assert peak>.01;track*=.89/peak
  with wave.open(str(work/'voice.wav'),'wb') as out:out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000);out.writeframes((track*32767).astype('<i2').tobytes())
  for i,(seg,_) in enumerate(plan):
   d=seg['duration'];card=seg['kind']+'.png';name=f'segment-{i}.mp4'
   codec=['-an','-c:v','libx264','-crf','18','-preset','fast','-pix_fmt','yuv420p','-threads','2','-t',str(d)]
   if seg['kind'].startswith('action-'):
    old=seg['source_end']-seg['source_start'];y=80+(704-round(original['height']*1184/1280/2)*2)/2
    graph=f"[0:v]trim=start={seg['source_start']}:end={seg['source_end']},setpts=(PTS-STARTPTS)*{d/old},scale=1184:-2:flags=lanczos,fps={FPS},tpad=stop_mode=clone:stop_duration=0.12,trim=duration={d},setsar=1[ui];[1:v][ui]overlay=48:{y}:shortest=1,format=yuv420p[v]"
    ffmpeg(tag,work,'-i','source.webm','-loop','1','-framerate',str(FPS),'-i',card,'-filter_complex_threads','1','-filter_complex',graph,'-map','[v]',*codec,name)
   else:
    graph=f"zoompan=z='1+.012*on/{round(d*FPS)}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d=1:s=1280x800:fps={FPS},format=yuv420p"
    ffmpeg(tag,work,'-loop','1','-framerate',str(FPS),'-i',card,'-vf',graph,*codec,name)
  args=[];filters=[]
  for i in range(len(plan)):args+=['-i',f'segment-{i}.mp4'];filters.append(f'[{i}:v]settb=AVTB,format=yuv420p[v{i}]')
  previous='v0'
  for i in range(1,len(plan)):
   target=f'join{i}';filters.append(f'[{previous}][v{i}]xfade=transition=fade:duration={FADE}:offset={plan[i][0]["start"]}[{target}]');previous=target
  ffmpeg(tag,work,*args,'-i','voice.wav','-filter_complex_threads','1','-filter_complex',';'.join(filters),'-map',f'[{previous}]','-map',f'{len(plan)}:a','-c:v','libx264','-crf','22','-preset','fast','-threads','2','-pix_fmt','yuv420p','-r',str(FPS),'-c:a','aac','-b:a','128k','-t',str(position),'-movflags','+faststart','-map_metadata','-1','lesson.mp4')
  ffmpeg(tag,work,'-i','lesson.mp4','-c:v','libvpx-vp9','-crf','34','-b:v','0','-row-mt','1','-cpu-used','5','-c:a','libopus','-b:a','96k','-map_metadata','-1','lesson.webm')
  v=copy.deepcopy(original);v.update(width=1280,height=800,duration_seconds=round(position,2));v['tutorial_source']={'file_sha256':original['file_sha256'],'width':original['width'],'height':original['height'],'duration_seconds':original['duration_seconds'],'narration':original['narration']}
  v['tutorial_source']['caption_strip_source']=v.pop('caption_strip_source',None)
  v['narration']={'voice':config['voice'],'language':'en-us','synthetic':True,'source_sha256':original['file_sha256'],'cues':[seg for seg,_ in plan]}
  files={};produced=work/'produced';produced.mkdir(exist_ok=True)
  for file,field,codec in [('lesson.mp4','fallback','aac'),('lesson.webm','file','opus')]:
   facts=probe(tag,work,file);video=next(s for s in facts['streams'] if s['codec_type']=='video');audio=next(s for s in facts['streams'] if s['codec_type']=='audio');assert (video['width'],video['height'])==(1280,800) and audio['codec_name']==codec and abs(float(facts['format']['duration'])-position)<.25
   ffmpeg(tag,work,'-v','error','-xerror','-i',file,'-f','null','-');assert (work/file).stat().st_size<15_000_000
   shutil.copyfile(work/file,produced/v[field]);v[field+'_sha256']=sha(produced/v[field])
  ffmpeg(tag,work,'-i','intro.png','-frames:v','1','-q:v','2','poster.jpg');shutil.copyfile(work/'poster.jpg',produced/v['poster']);v['poster_sha256']=sha(produced/v['poster'])
  (produced/v['captions']).write_text(webvtt(v['narration']['cues'],position));v['captions_sha256']=sha(produced/v['captions'])
  for n in [v[k] for k in ['file','fallback','poster','captions']]:files[n]=sha(produced/n);shutil.copyfile(produced/n,a.output/n)
  receipt.write_text(json.dumps({'inputs':identity,'files':files,'video':v},indent=2)+'\n');result['videos'].append(v);print('Produced lesson:',key,round(position,2),'seconds; complete MP4/WebM decode passed',flush=True)
 (a.output/'videos.json').write_text(json.dumps(result,indent=2)+'\n')
if __name__=='__main__':main()
