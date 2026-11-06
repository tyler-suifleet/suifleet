#!/usr/bin/env python3
"""Owner-controlled Pages provisioning. No token output, DNS writes or paid services."""
import argparse,json,os,re,urllib.error,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def api(method,path,payload=None):
    config=Path.home()/'.config/.wrangler/config/default.toml'
    match=re.search(r'^oauth_token\s*=\s*("[^"\n]+")',config.read_text(),re.M)
    if not match:raise ValueError('Sign in with the pinned Wrangler CLI first')
    token=json.loads(match[1])
    request=urllib.request.Request('https://api.cloudflare.com/client/v4'+path,method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=45) as response:result=json.load(response)
    except urllib.error.HTTPError as error:
        result=json.loads(error.read());raise RuntimeError('Cloudflare '+str(error.code)+': '+str([(e.get('code'),e.get('message')) for e in result.get('errors',[])])) from None
    if not result.get('success'):raise RuntimeError('Cloudflare refused the request')
    return result['result']

def require(condition,message):
    if not condition:raise ValueError(message)

def validate(config):
    require(config['schema']==1 and config['zone_name']=='suifleet.io','Unexpected zone/schema')
    require(re.fullmatch('[0-9a-f]{32}',config['account_id']) and re.fullmatch('[0-9a-f]{32}',config['zone_id']),'Invalid account/zone ID')
    require(set(config['projects'])=={'website','app','docs'},'All three project roles are required')
    expected={'website':['suifleet.io','www.suifleet.io'],'app':['app.suifleet.io'],'docs':['docs.suifleet.io']}
    for role,p in config['projects'].items():
        require(re.fullmatch('suifleet-[a-z-]+',p['name']) and p['domains']==expected[role],'Unexpected project/domain scope')
    require(config['production_branch']=='production','Unexpected production branch')

def provision(config,request=api):
    validate(config)
    zone=request('GET','/zones/'+config['zone_id'])
    if zone['name']!=config['zone_name'] or zone['account']['id']!=config['account_id'] or zone['status']!='active':
        raise ValueError('The authenticated account must own the active suifleet.io zone')
    base='/accounts/'+config['account_id']+'/pages/projects'
    existing={p['name']:p for p in request('GET',base)}
    projects={}
    for role,plan in config['projects'].items():
        p=existing.get(plan['name'])
        if p is None:p=request('POST',base,{'name':plan['name'],'production_branch':config['production_branch']})
        if p['name']!=plan['name'] or p['production_branch']!=config['production_branch'] or p.get('source'):
            raise ValueError('Unexpected project identity/branch or existing Git integration')
        if not re.fullmatch('[a-z0-9-]+\.pages\.dev',p['subdomain']):raise ValueError('Unexpected provider-returned Pages domain')
        projects[role]={k:p[k] for k in ['id','name','subdomain','production_branch']}
    return {'schema':1,'account_id':config['account_id'],'zone_id':config['zone_id'],'projects':projects}

def bindings(config,receipt,request=api):
    validate(config)
    if receipt['account_id']!=config['account_id'] or receipt['zone_id']!=config['zone_id']:raise ValueError('Receipt account/zone mismatch')
    output=[]
    for role,p in receipt['projects'].items():
        require(p['name']==config['projects'][role]['name'],'Project identity mismatch')
        base='/accounts/'+config['account_id']+'/pages/projects/'+p['name']+'/domains'
        existing={d['name']:d for d in request('GET',base)}
        for name in config['projects'][role]['domains']:
            d=existing.get(name)
            if d is None:d=request('POST',base,{'name':name})
            if d['name']!=name:raise ValueError('Unexpected domain binding')
            output.append({'project':p['name'],'name':name,'status':d['status'],'details':{k:d[k] for k in ['id','zone_tag','validation_data','verification_data'] if k in d}})
    return output

def dns_zone(config,receipt):
    validate(config)
    if receipt['account_id']!=config['account_id'] or receipt['zone_id']!=config['zone_id']:raise ValueError('Receipt account/zone mismatch')
    lines=['; Cloudflare Pages web records only. Preserve mail and all unrelated records.', '; Add the custom domains in Pages before applying these CNAME records.', '$ORIGIN suifleet.io.', '$TTL 300']
    for role,plan in config['projects'].items():
        p=receipt['projects'][role]
        if p['name']!=plan['name'] or not re.fullmatch('[a-z0-9-]+\.pages\.dev',p['subdomain']):raise ValueError('Unverified Pages project target')
        for domain in plan['domains']:
            name='@' if domain=='suifleet.io' else domain.removesuffix('.suifleet.io')
            lines.append(f'{name} IN CNAME {p["subdomain"]}. ; cf_tags=cf-proxied:true')
    return '\n'.join(lines)+'\n'

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['provision','bind','dns']);p.add_argument('--receipt',type=Path,required=True);p.add_argument('--output',type=Path);a=p.parse_args()
    config=json.loads((ROOT/'cloudflare-pages.json').read_text())
    if a.action=='provision':
        result=provision(config);a.receipt.write_text(json.dumps(result,indent=2)+'\n');print('Verified owner account and Pages projects:',a.receipt)
    else:
        receipt=json.loads(a.receipt.read_text());result=bindings(config,receipt) if a.action=='bind' else dns_zone(config,receipt)
        if not a.output:raise ValueError('--output is required')
        a.output.write_text((json.dumps(result,indent=2)+'\n') if not isinstance(result,str) else result);print('Wrote:',a.output)
if __name__=='__main__':main()
