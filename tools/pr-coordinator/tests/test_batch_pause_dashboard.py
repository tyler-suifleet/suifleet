import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard as d


class RecoveryDashboard(unittest.TestCase):
    def test_pause_projection_precedes_generic_missing_source_evidence(self):
        snapshot={'mtime':100,'batch_pause':'waiting-installed-repair'}
        stage,_=d.pipeline_stage(snapshot,None,now=101)
        self.assertEqual(stage,'batch-recovery-paused')
        self.assertEqual(d.pipeline_stage(snapshot,None,now=101,active=True)[0],'actively-working')

    def test_only_known_public_pause_codes_survive_snapshot_projection(self):
        basic=dict(number=1,headRefOid='a'*40,baseRefOid='b'*40,statusCheckRollup=[])
        for kind in ('waiting-installed-repair','disabled-with-active-batch'):
            self.assertEqual(d.project_snapshot({**basic,'_batch_pause':kind},100)['batch_pause'],kind)
        self.assertIsNone(d.project_snapshot({**basic,'_batch_pause':'PRIVATE_REASON_CANARY'},100)['batch_pause'])

    def test_newer_complete_inventory_removes_closed_history_but_not_new_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);paths={'inventory':root};config={'projects':[{'id':'p','repository':'a/b'}]}
            d.inventory_path(root,'p').write_text(json.dumps(dict(schema=1,project='p',repository='a/b',fetched_at=100,
                prs=[dict(number=1,title='open',url='https://github.com/a/b/pull/1')])) )
            snapshots={('p',1):{'mtime':90},('p',2):{'mtime':90},('p',3):{'mtime':101},('other',1):{'mtime':1}}
            visible=d.visible_pipeline_snapshots(snapshots,paths,config,110,None)
            self.assertEqual(set(visible),{('p',1),('p',3),('other',1)})
            self.assertIn(('p',2),snapshots)
            self.assertEqual(d.visible_pipeline_snapshots(snapshots,paths,config,2000,None),snapshots)

    def test_mismatched_or_malformed_inventory_does_not_hide_prs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);paths={'inventory':root};config={'projects':[{'id':'p','repository':'a/b'}]};snapshots={('p',1):{'mtime':90}}
            for data in [dict(schema=1,project='p',repository='wrong/repo',fetched_at=100,prs=[]),
                         dict(schema=1,project='p',repository='a/b',fetched_at=100,prs='bad')]:
                d.inventory_path(root,'p').write_text(json.dumps(data))
                self.assertEqual(d.visible_pipeline_snapshots(snapshots,paths,config,110,None),snapshots)


if __name__=='__main__':unittest.main()
