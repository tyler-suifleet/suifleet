import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from batch_integration import local_scope

class TrackingScope(unittest.TestCase):
    def setUp(self):
        self.owner={'key':'SF-263','spec':{'key':'SF-263','area':'infra'},'remote':{'url':'https://github.com/owner/repo/issues/689','item_id':'item'}}
        self.before=[{'repository':'owner/repo','issues':[{'key':'SF-262'}]}, {'repository':'owner/repo','issues':{'SF-262':{'number':1}}}]
        self.after=copy.deepcopy(self.before)
        self.after[0]['issues'].append(self.owner['spec'])
        self.after[1]['issues']['SF-263']={'number':689,'url':self.owner['remote']['url'],'project_item_id':'item','id':1,'node_id':'node'}
        self.paths=['tools/pr-coordinator/coordinator.py','docs/tracking/backlog.json','docs/tracking/github-state.json']
    def git(self,*args):
        if args[0]=='diff':return '\n'.join(self.paths)
        ref,path=args[1].split(':',1)
        return json.dumps((self.before if ref=='base' else self.after)[int(path.endswith('github-state.json'))])
    def scope(self):return local_scope(self.git,'owner/repo','base','head',self.owner)
    def test_exact_published_own_ticket_append(self):
        result=self.scope(); self.assertEqual(result['tracking'],self.owner)
    def test_previous_ticket_mutation_rejected(self):
        self.after[0]['issues'][0]['title']='changed';self.assertIsNone(self.scope())
    def test_other_ticket_or_metadata_rejected(self):
        self.after[0]['issues'].append({'key':'OTHER'});self.assertIsNone(self.scope())
        self.after=copy.deepcopy(self.before); self.after[0]['repository']='elsewhere';self.assertIsNone(self.scope())
    def test_wrong_remote_and_application_code_rejected(self):
        self.after[1]['issues']['SF-263']['project_item_id']='different';self.assertIsNone(self.scope())
        self.after[1]['issues']['SF-263']['project_item_id']='item'
        self.paths.append('dapp/src/page.tsx');self.assertIsNone(self.scope())
    def test_existing_remote_record_edit_rejected(self):
        self.after[1]['issues']['SF-262']['number']=2;self.assertIsNone(self.scope())
    def test_one_seed_alone_rejected(self):
        self.paths.remove('docs/tracking/github-state.json');self.assertIsNone(self.scope())
