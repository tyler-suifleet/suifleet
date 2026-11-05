import importlib.util,json,unittest
from pathlib import Path
ROOT=Path(__file__).parent
spec=importlib.util.spec_from_file_location('host_dns',ROOT/'dns.py');dns=importlib.util.module_from_spec(spec);spec.loader.exec_module(dns)
class DNSTest(unittest.TestCase):
 def test_exact_provider_records_and_no_mail_mutations(self):
  bindings=json.loads((ROOT/'domain-bindings.json').read_text());text=dns.zone(bindings)
  self.assertEqual(text,(ROOT/'dns-records.zone').read_text());lines=[l for l in text.splitlines() if ' IN ' in l];self.assertEqual(len(lines),13)
  self.assertEqual(sum(' IN TXT ' in l for l in lines),8);self.assertEqual(sum('cf-proxied:false' in l for l in lines),5)
  self.assertFalse(any(' IN MX ' in l or ' IN NS ' in l or '_domainkey' in l for l in lines))
  for b in bindings:
   for v in b['validation_records']:self.assertIn(json.dumps(v['value']),text)
 def test_refuse_mail_or_unrelated_domain_scope(self):
  b=json.loads((ROOT/'domain-bindings.json').read_text());b[0]['validation_records'][0]['name']='google._domainkey.suifleet.io'
  with self.assertRaises(ValueError):dns.zone(b)
  b=json.loads((ROOT/'domain-bindings.json').read_text());b[0]['hostname']='different.example'
  with self.assertRaises(ValueError):dns.zone(b)
if __name__=='__main__':unittest.main()
