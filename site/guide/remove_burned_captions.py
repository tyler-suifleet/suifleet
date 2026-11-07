#!/usr/bin/env python3
"""Reframe historical guide clips to exclude their annotation strip; preserve speech/timing."""
import argparse,copy,hashlib,json,shutil,subprocess,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from encode_guide import image,ffmpeg
from narrate_guide import probe
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def audio_hash(tag,root,name):
 r=subprocess.check_output(['docker','run','--rm','--network','none','-v',str(root.resolve())+':/work','--entrypoint','ffmpeg',tag,'-v','error','-i','/work/'+name,'-map','0:a:0','-c:a','pcm_s16le','-f','hash','-hash','sha256','pipe:1'],text=True)
 return r.strip()
def main():
 p=argparse.ArgumentParser();p.add_argument('--source-media',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--reuse-media',type=Path);a=p.parse_args()
 if a.output.exists() or a.output.resolve()==a.source_media.resolve():raise ValueError('Output must be new and separate')
 manifest=json.loads((a.source_media/'videos.json').read_text());assert 'caption_strip_removal' not in manifest
 for v in manifest['videos']:
  assert (v['width'],v['height'])==(1280,800)
  for key in ['file','fallback','poster','captions']:
   name=v[key];assert Path(name).name==name and sha(a.source_media/name)==v[key+'_sha256']
 cache=json.loads((a.reuse_media/'videos.json').read_text()) if a.reuse_media else None
 if cache:assert cache['caption_strip_removal']['source_manifest_sha256']==sha(a.source_media/'videos.json'),'Unrelated cached source'
 cached={v['step']:v for v in cache['videos']} if cache else {}
 a.output.mkdir(parents=True);tag=image();result=copy.deepcopy(manifest)
 result['caption_strip_removal']={'script_sha256':sha(Path(__file__)),'source_manifest_sha256':sha(a.source_media/'videos.json'),'crop_bottom_pixels':{'default':40,'deploy-group':60,'staged-rollout':60},'note':'Historical viewport reframed to exclude the original caption strip; not fresh console acceptance.'}
 if cache:result['caption_strip_removal']['reused_manifest_sha256']=sha(a.reuse_media/'videos.json')
 for i,v in enumerate(result['videos']):
  strip=60 if v['step'] in {'deploy-group','staged-rollout'} else 40
  prior=cached.get(v['step'])
  if prior and prior['height']==v['height']-strip:
   for key in ['file','fallback','poster','captions']:
    assert sha(a.reuse_media/prior[key])==prior[key+'_sha256'],'Cached media changed'
    if key in {'file','fallback'}:
     assert prior['caption_strip_source'][key+'_sha256']==v[key+'_sha256']
     assert audio_hash(tag,a.reuse_media,prior[key])==audio_hash(tag,a.source_media,v[key])
    shutil.copyfile(a.reuse_media/prior[key],a.output/prior[key])
   result['videos'][i]=copy.deepcopy(prior)
   result['videos'][i]['caption_strip_source']['transformation_script_sha256']=cache['caption_strip_removal']['script_sha256']
   result['videos'][i]['caption_strip_source']['crop_bottom_pixels']=strip
   print('Reused verified reframed media:',v['step'],flush=True);continue
  v['caption_strip_source']={k:v[k] for k in ['file_sha256','fallback_sha256','poster_sha256','width','height']};v['caption_strip_source'].update(crop_bottom_pixels=strip,transformation_script_sha256=sha(Path(__file__)));v['height']-=strip
  for key in ['file','fallback']:
   name=v[key];source=a.source_media/name;work=a.output;input_name='input'+source.suffix;shutil.copyfile(source,work/input_name)
   codec=['-c:v','libvpx-vp9','-crf','32','-b:v','0','-row-mt','1','-cpu-used','5'] if key=='file' else ['-c:v','libx264','-crf','20','-preset','fast','-movflags','+faststart']
   before=probe(tag,work,input_name);original_audio=audio_hash(tag,work,input_name)
   ffmpeg(tag,work,'-i',input_name,'-vf',f'crop=iw:ih-{strip}:0:0',*codec,'-c:a','copy',name)
   after=probe(tag,work,name);stream=next(s for s in after['streams'] if s['codec_type']=='video');assert (stream['width'],stream['height'])==(1280,v['height']);assert abs(float(before['format']['duration'])-float(after['format']['duration']))<.15
   assert audio_hash(tag,work,name)==original_audio,'Speech changed';ffmpeg(tag,work,'-v','error','-xerror','-i',name,'-f','null','-');assert (work/name).stat().st_size<15_000_000
   v[key+'_sha256']=sha(work/name);(work/input_name).unlink()
  shutil.copyfile(a.source_media/v['poster'],a.output/'input.jpg');ffmpeg(tag,a.output,'-i','input.jpg','-vf',f'crop=iw:ih-{strip}:0:0','-frames:v','1','-q:v','2',v['poster']);(a.output/'input.jpg').unlink();v['poster_sha256']=sha(a.output/v['poster'])
  shutil.copyfile(a.source_media/v['captions'],a.output/v['captions']);print('Reframed, audio-identical, fully decoded:',v['step'],flush=True)
 (a.output/'videos.json').write_text(json.dumps(result,indent=2)+'\n')
if __name__=='__main__':main()
