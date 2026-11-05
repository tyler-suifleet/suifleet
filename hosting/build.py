#!/usr/bin/env python3
"""Reproducible hosted static bundles. No keys, chain mutations, or DNS writes."""
import argparse,hashlib,importlib.util,json,os,shlex,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def module(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
def add_dns_download(website_out, zone_source=None):
 if zone_source is None: raise ValueError('Supply the chosen provider DNS file explicitly')
 directory=website_out/'setup';directory.mkdir()
 (directory/'suifleet-dns.zone').write_bytes(zone_source.read_bytes())
 with (website_out/'_headers').open('a') as stream:
  stream.write('\n/setup/suifleet-dns.zone\n  Content-Type: text/plain; charset=utf-8\n  Content-Disposition: attachment; filename="suifleet-dns.zone"\n  X-Robots-Tag: noindex\n')
def main():
 p=argparse.ArgumentParser();p.add_argument('--origins',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--node',type=Path,required=True);p.add_argument('--dns-zone',type=Path);a=p.parse_args()
 if a.out.exists():raise ValueError('Output must be a new immutable directory')
 website=module('hosting_website',ROOT/'site/build.py');config=website.load_config(a.origins);origins={k:config[k] for k in ['origin','app_url','docs_url']};revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
 env=dict(os.environ)
 for line in (ROOT/'profiles/seal-testnet/frontend.env').read_text().splitlines():
  if not line or line.startswith('#'):continue
  key,value=line.split('=',1)
  if not key.startswith('NEXT_PUBLIC_'):raise ValueError('Refuse nonpublic frontend configuration')
  parts=shlex.split(value)
  if len(parts)!=1:raise ValueError('Expected one nonempty frontend value: '+key)
  env[key]=parts[0]
 env['NEXT_PUBLIC_DEVICE_DOCS_BASE']=origins['docs_url'];env['PATH']=str(a.node.parent)+os.pathsep+env['PATH']
 version=subprocess.check_output([str(a.node),'--version'],text=True).strip()
 if version!='v22.22.2':raise ValueError('Use pinned Node22.22.2')
 subprocess.run([str(a.node),'scripts/check_profiles.cjs'],cwd=ROOT,env=env,check=True)
 subprocess.run([str(a.node),'node_modules/next/dist/bin/next','build','--webpack'],cwd=ROOT/'dapp',env=env,check=True)
 manifest=ROOT/'profiles/seal-testnet/deployment.json';(ROOT/'dapp/out/deployment-manifest.json').write_bytes(manifest.read_bytes())
 prepare=module('hosting_prepare',ROOT/'site/prepare_hosted.py');a.out.mkdir(parents=True)
 prepare.prepare(ROOT/'dapp/out',a.out/'app','console',revision)
 docs=module('hosting_docs',ROOT/'site/docs/build.py');docs.build(a.out/'docs-source',{'origin':origins['docs_url'].rstrip('/'),'website_url':origins['origin'],'app_url':origins['app_url']})
 prepare.prepare(a.out/'docs-source',a.out/'docs','docs',revision)
 website.build(a.out/'website',config)
 if a.dns_zone:add_dns_download(a.out/'website',a.dns_zone)
 subprocess.run([str(a.node),'scripts/check_profiles.cjs'],cwd=ROOT,env=env,check=True)
 receipt={'source_commit':revision,'node':version,'network':'testnet','origins':origins,'configuration_sha256':{n:hashlib.sha256((ROOT/'profiles/seal-testnet'/n).read_bytes()).hexdigest() for n in ['deployment.json','frontend.env']},'roles':{}}
 for role in ['website','app','docs']:
  files={str(f.relative_to(a.out/role)):{'bytes':f.stat().st_size,'sha256':hashlib.sha256(f.read_bytes()).hexdigest()} for f in (a.out/role).rglob('*') if f.is_file()};receipt['roles'][role]=files
 (a.out/'bundle-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print('Verified hosted bundles:',a.out)
if __name__=='__main__':main()
