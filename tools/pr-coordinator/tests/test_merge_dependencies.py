"""Merge prerequisites must not stop authorized development or strand launch intent."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_coordinator import c, fixture


class MergeDependencies(unittest.TestCase):
    def engine(self, directory):
        return c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},Path(directory))

    def test_development_dependency_is_checked_before_reserve_or_resume(self):
        for fields in ({'spec':{'depends_on':['T0']}},{'remote':{'depends_on':['T0']}}):
            with self.subTest(fields=fields),tempfile.TemporaryDirectory() as directory:
                e=self.engine(directory);pr,o,_=fixture();o.update(key='T1',token='t',**fields)
                with patch.object(e,'ticket',return_value=json.dumps({'status':'review'})) as ticket, \
                     patch.object(e.store,'reserve',return_value=None) as reserve,patch.object(c.subprocess,'Popen') as spawn:
                    e.launch({'id':'test'},pr,o,'fix')
                    reserve.assert_not_called();spawn.assert_not_called()
                    self.assertEqual(ticket.call_args.args[1:],('show','T0'))

    def test_external_development_blocker_never_reserves(self):
        with tempfile.TemporaryDirectory() as directory:
            e=self.engine(directory);pr,o,_=fixture();o['remote']={'external_blockers':['https://example.test/issue']}
            with patch.object(e.store,'reserve',return_value=None) as reserve:
                e.launch({'id':'test'},pr,o,'fix');reserve.assert_not_called()

    def test_merge_only_dependency_allows_development_but_refuses_integration(self):
        with tempfile.TemporaryDirectory() as directory:
            e=self.engine(directory);pr,o,_=fixture();o.update(key='T1',token='t',spec={'merge_depends_on':['T0']})
            with patch.object(e,'ticket') as ticket:
                self.assertIsNone(e.development_blocker({},o));ticket.assert_not_called()
            with patch.object(e,'ticket',side_effect=[json.dumps(o),json.dumps({'status':'review'})]):
                with self.assertRaisesRegex(RuntimeError,'dependency'):e.assert_review_owner({},o)
            with patch.object(e,'ticket',side_effect=[json.dumps(o),json.dumps({'status':'done','publish_pending':True})]):
                with self.assertRaisesRegex(RuntimeError,'dependency'):e.assert_review_owner({},o)
            with patch.object(e,'ticket',side_effect=[json.dumps(o),json.dumps({'status':'done','publish_pending':False})]), \
                 patch.object(e,'gh',return_value='{"state":"open"}'):
                e.assert_review_owner({'repository':'owner/repo'},o)
