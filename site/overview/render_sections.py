#!/usr/bin/env python3
"""Cut narrated chapter clips from the verified overview; no new footage or speech."""
import argparse,hashlib,json,shutil,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'site/guide'))
from encode_guide import ffmpeg,image
from captions import webvtt
from narrate_guide import probe
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
CHAPTERS=[('first-update','Approvals before an update',12,24),('update-flow','From publishing to a device result',24,84),('your-platform','Your installer stays in control',60,72),('behind-console','Shared records, storage and device clients',24,48)]
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--runtime',type=Path,required=True);args=parser.parse_args()
 media=ROOT/'site/media/overview';source=json.loads((media/'manifest.json').read_text());video=next(v for v in source['variants'] if v['name']=='landscape');original=media/video['fallback'];assert sha(original)==video['fallback_sha256']
 args.runtime.mkdir(parents=True,exist_ok=True);shutil.copyfile(original,args.runtime/'source.mp4');tag=image();entries=[]
 for section,title,start,end in CHAPTERS:
  duration=end-start;name=f'section-{section}';filename=name+'.mp4'
  ffmpeg(tag,args.runtime,'-ss',str(start),'-i','source.mp4','-t',str(duration),'-c:v','libx264','-preset','fast','-crf','21','-threads','2','-pix_fmt','yuv420p','-c:a','aac','-b:a','128k','-movflags','+faststart','-map_metadata','-1',filename)
  facts=probe(tag,args.runtime,filename);assert abs(float(facts['format']['duration'])-duration)<.15
  ffmpeg(tag,args.runtime,'-v','error','-xerror','-i',filename,'-f','null','-')
  poster=name+'.jpg';ffmpeg(tag,args.runtime,'-ss','1','-i',filename,'-frames:v','1','-q:v','2',poster)
  cues=[{**cue,'speech_start':round(cue['speech_start']-start,6),'speech_end':round(cue['speech_end']-start,6)} for cue in source['cues'] if start<=cue['speech_start']<cue['speech_end']<=end]
  captions=name+'.en.vtt';(args.runtime/captions).write_text(webvtt(cues,duration))
  entry=dict(section=section,title=title,start=start,end=end,duration_seconds=duration,width=1280,height=720,cues=cues)
  for key,file in [('file',filename),('poster',poster),('captions',captions)]:shutil.copyfile(args.runtime/file,media/file);entry[key]=file;entry[key+'_sha256']=sha(media/file)
  entries.append(entry);print(section,duration,'seconds; full decode passed',flush=True)
 result=dict(schema=1,kind='overview-chapter-excerpts',source_manifest_sha256=sha(media/'manifest.json'),source_video_sha256=sha(original),renderer_sha256=sha(Path(__file__)),clips=entries)
 (media/'sections.json').write_text(json.dumps(result,indent=2)+'\n')
if __name__=='__main__':main()
