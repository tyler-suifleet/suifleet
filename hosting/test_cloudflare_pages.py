import copy,importlib.util,json,unittest
from pathlib import Path
ROOT=Path(__file__).parent
spec=importlib.util.spec_from_file_location('cf_pages',ROOT/'cloudflare_pages.py');cf=importlib.util.module_from_spec(spec);spec.loader.exec_module(cf)
class PagesTest(unittest.TestCase):
 def setUp(self):
  self.config=json.loads((ROOT/'cloudflare-pages.json').read_text());self.receipt=json.loads((ROOT/'cloudflare-projects.json').read_text())
 def test_wrong_zone_owner_never_creates_a_project(self):
  calls=[]
  def request(method,path,payload=None):
   calls.append(method);return {'name':'suifleet.io','account':{'id':'0'*32},'status':'active'}
  with self.assertRaisesRegex(ValueError,'must own'):cf.provision(self.config,request)
  self.assertEqual(calls,['GET'])
 def test_existing_projects_are_reused_without_mutation(self):
  calls=[]
  def request(method,path,payload=None):
   calls.append(method)
   if path.startswith('/zones/'):return {'name':'suifleet.io','account':{'id':self.config['account_id']},'status':'active'}
   return list(self.receipt['projects'].values())
  self.assertEqual(cf.provision(self.config,request),self.receipt);self.assertEqual(calls,['GET','GET'])
 def test_wrong_branch_or_git_integrated_project_refused(self):
  for mutation in [{'production_branch':'other'},{'source':{'type':'github'}}]:
   data=copy.deepcopy(self.receipt);data['projects']['website'].update(mutation)
   def request(method,path,payload=None):
    if path.startswith('/zones/'):return {'name':'suifleet.io','account':{'id':self.config['account_id']},'status':'active'}
    return list(data['projects'].values())
   with self.assertRaisesRegex(ValueError,'Unexpected project'):cf.provision(self.config,request)
 def test_dns_uses_only_actual_verified_pages_targets(self):
  text=cf.dns_zone(self.config,self.receipt)
  self.assertEqual(sum(' IN CNAME ' in x for x in text.splitlines()),4)
  self.assertNotIn('OpenAI',text);self.assertNotIn(' IN MX ',text);self.assertNotIn(' IN TXT ',text)
  for target in self.receipt['projects'].values():self.assertIn(target['subdomain']+'.',text)
  changed=copy.deepcopy(self.receipt);changed['projects']['app']['subdomain']='attacker.example'
  with self.assertRaisesRegex(ValueError,'Unverified'):cf.dns_zone(self.config,changed)
 def test_domain_binding_refuses_wrong_account_before_network(self):
  changed=copy.deepcopy(self.receipt);changed['account_id']='0'*32
  with self.assertRaisesRegex(ValueError,'Receipt account'):cf.bindings(self.config,changed,lambda *args:self.fail('Must not call API'))
if __name__=='__main__':unittest.main()
