#!/usr/bin/env python3
"""Generate only provider-issued SuiFleet web records; never edits DNS."""
import argparse,ipaddress,json,re
from pathlib import Path
HOSTS={'suifleet.io','www.suifleet.io','app.suifleet.io','docs.suifleet.io'}
def zone(bindings):
 if {b['hostname'] for b in bindings} != HOSTS:raise ValueError('Unexpected domain scope')
 lines=['; Web hosting records only. Preserve Google Workspace MX/TXT/DKIM and all unrelated records.','; For an existing app record, edit its target/type rather than creating conflicting records.','$ORIGIN suifleet.io.','$TTL 300']
 for b in bindings:
  host=b['hostname'];name='@' if host=='suifleet.io' else host.removesuffix('.suifleet.io')
  if name=='@':
   for ip in b['apex_proxy_ipv4_targets']:lines.append('@ IN A '+str(ipaddress.IPv4Address(ip))+' ; cf_tags=cf-proxied:false')
  else:
   target=b['cname_target']
   if not re.fullmatch(r'[A-Za-z0-9.-]+\.',target):raise ValueError('Invalid provider target')
   lines.append(name+' IN CNAME '+target+' ; cf_tags=cf-proxied:false')
  for v in b['validation_records']:
   if v['record_type']!='TXT' or v['name'] not in {'_openai-site-verification.'+host,'_cf-custom-hostname.'+host}:raise ValueError('Unexpected verification record')
   lines.append(v['name']+'. IN TXT '+json.dumps(v['value']))
 return '\n'.join(lines)+'\n'
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.write_text(zone(json.loads((Path(__file__).parent/'domain-bindings.json').read_text())))
