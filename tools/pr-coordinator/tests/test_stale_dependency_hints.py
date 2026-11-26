"""Offline regressions for the November 26 stale dependency handoffs."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import dispatcher as d
import recovery_watchdog as w


class ReceiptTests(unittest.TestCase):
    def test_structured_intent_survives_legacy_diagnostic_serialization(self):
        for evidence in ('blocked until merge', {'blocker':'needs merge'}, ['needs merge']):
            result=dict(verdict='blocked',evidence=evidence,
                        dependency_wait=dict(tickets=['SF-295'],integration='merged_main'))
            self.assertEqual(d.dependency_wait(result,'SF-306'),['SF-295'])

    def test_prose_or_malformed_intent_never_grants_resume(self):
        result=dict(verdict='blocked',evidence={'blocker':'SF-295 merged; run hardware'})
        self.assertIsNone(d.dependency_wait(result,'SF-306'))
        for wait in (dict(tickets=['SF-295'],integration='hardware'),
                     dict(tickets=['SF-306'],integration='merged_main'),
                     dict(tickets=['other::SF-295'],integration='merged_main'),
                     dict(tickets=['SF-295'],integration='merged_main',extra='credential')):
            self.assertIsNone(d.dependency_wait(dict(result,dependency_wait=wait),'SF-306'))
        for value in (None,[],True,{}):
            self.assertIsNone(d.dependency_wait(value,'SF-306'))
        for evidence in (None,False,0,[],{},'  '):
            self.assertIsNone(d.dependency_wait(dict(result,evidence=evidence,
                dependency_wait=dict(tickets=['SF-295'],integration='merged_main')),'SF-306'))


class HintTests(unittest.TestCase):
    def setUp(self):
        self.rows=[dict(key='SF-311',status='blocked',publish_pending=False),
                   dict(key='SF-295',status='done',publish_pending=False)]
        self.detail=dict(key='SF-311',project='p',status='blocked',agent='Codex',
                         session='old-session',branch='work/old',publish_pending=False,
                         token='PRIVATE-COORDINATION-TOKEN',
                         evidence='needs corrected source archive',
                         spec=dict(depends_on=[]),
                         remote=dict(depends_on=['p::SF-295'],external_blockers=[]))

    def hints(self,detail=None,rows=None,excluded=()):
        reader=Mock(return_value=copy.deepcopy(self.detail if detail is None else detail))
        result=w.completed_dependency_hints(self.rows if rows is None else rows,'p',reader,excluded)
        return result,reader

    def test_completed_declared_dependencies_are_private_discovery_only(self):
        hints,reader=self.hints()
        self.assertEqual(hints[0]['ticket'],'SF-311')
        self.assertEqual(hints[0]['dependencies'],['SF-295'])
        self.assertEqual(set(hints[0]),{'ticket','dependencies','receipt'})
        self.assertNotIn('PRIVATE',json.dumps(hints))
        self.assertNotIn('work/old',json.dumps(hints))
        self.assertNotIn('needs corrected',json.dumps(hints))
        self.assertLessEqual(reader.call_args.args[1],1)
        again,_=self.hints()
        self.assertEqual(hints,again)
        changed=copy.deepcopy(self.detail);changed['evidence']='new handoff'
        self.assertNotEqual(hints,self.hints(changed)[0])

    def test_live_foreign_external_unassigned_or_ambiguous_waits_are_not_hints(self):
        variants=[]
        for update in (dict(key='SF-312'),dict(project='other'),dict(status='in_progress'),
                       dict(agent=None),dict(publish_pending=True)):
            variants.append(dict(self.detail,**update))
        for remote in (dict(depends_on=['other::SF-295']),
                       dict(depends_on=['p::SF-295'],external_blockers=['provider']),
                       dict(depends_on=['p::SF-999']),dict(depends_on=['SF-311']),
                       dict(depends_on=[]),dict(depends_on=None)):
            variants.append(dict(self.detail,remote=remote))
        for detail in variants:
            with self.subTest(detail=detail):
                self.assertEqual(self.hints(detail)[0],[])
        for change in (dict(status='review'),dict(publish_pending=True)):
            rows=copy.deepcopy(self.rows);rows[1].update(change)
            self.assertEqual(self.hints(rows=rows)[0],[])
        result,reader=self.hints(excluded=['SF-311'])
        self.assertEqual(result,[]);reader.assert_not_called()

    def test_unavailable_details_and_time_budget_do_not_leak_or_block(self):
        reader=Mock(side_effect=subprocess.CalledProcessError(1,['ticket'],stderr='PRIVATE'))
        self.assertEqual(w.completed_dependency_hints(self.rows,'p',reader),[])
        reader=Mock()
        with patch.object(w.time,'monotonic',return_value=10):
            self.assertEqual(w.completed_dependency_hints(self.rows,'p',reader,deadline=9),[])
        reader.assert_not_called()
        self.assertEqual(w.completed_dependency_hints(None,'p',reader),[])


class PartialStallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config=dict(projects=[dict(id='p',root=str(self.root))],workers={'Codex':['true']},
                         recovery_watchdog=dict(enabled=True,projects=['p'],stall_seconds=60,
                                                cooldown_seconds=300,timeout_seconds=120))
        self.hint=dict(ticket='SF-311',dependencies=['SF-295'],receipt='a'*64)
        self.model=dict(inventory={'p':dict(state='ok',breakdown=dict(blocked=1,review=1))},
                        queue={'p':dict(done=5)},
                        workers=[dict(id='live',project='p',ticket='SF-999',role='implementation')],
                        attention=[],_watchdog_dependency_hints={'p':[self.hint]})

    def tick(self,now):
        return w.Watchdog(self.config,self.root,now).tick(self.model)

    def test_busy_workers_and_unrelated_completions_do_not_hide_aged_wait(self):
        self.assertIn('observation grace',self.tick(1000)['status'])
        self.model['queue']['p']['done']=6
        report=self.tick(1040)
        self.assertEqual(report['next_check_at'],1060)
        report=self.tick(1061)
        self.assertIn('would launch',report['status'])
        self.assertIn('completed declared dependencies',report['candidates'][0]['reason'])

    def test_new_wait_does_not_reset_an_older_ticket_clock(self):
        self.tick(1000)
        self.model['_watchdog_dependency_hints']['p'].append(
            dict(ticket='SF-324',dependencies=['SF-322'],receipt='b'*64))
        self.model['queue']['p']['done']=6
        self.tick(1040)
        self.assertIn('would launch',self.tick(1061)['status'])

    def test_temporary_detail_loss_does_not_reset_a_blocked_handoff_clock(self):
        self.model['_watchdog_tickets']={'p':[dict(key='SF-311',status='blocked')]}
        self.tick(1000)
        self.model['_watchdog_dependency_hints']['p']=[]
        self.model['queue']['p']['done']=6
        self.assertIn('workers active',self.tick(1040)['status'])
        self.model['_watchdog_dependency_hints']['p']=[self.hint]
        self.assertIn('would launch',self.tick(1061)['status'])

    def test_confirmed_ticket_resolution_removes_its_old_wait_clock(self):
        self.tick(1000)
        self.model['_watchdog_dependency_hints']['p']=[]
        self.model['_watchdog_tickets']={'p':[dict(key='SF-311',status='done')]}
        self.tick(1040)
        state=json.loads((self.root/'recovery-watchdog/state.json').read_text())
        self.assertEqual(state['observations']['p']['dependency_waits'],{})

    def test_changed_handoff_restarts_its_own_grace(self):
        self.tick(1000)
        self.model['_watchdog_dependency_hints']['p'][0]['receipt']='c'*64
        self.model['queue']['p']['done']=6
        self.tick(1040)
        self.assertIn('observation grace',self.tick(1061)['status'])
        self.assertIn('would launch',self.tick(1101)['status'])

    def test_acceptance_only_and_completed_waits_do_not_launch_idle_models(self):
        self.config['projects'][0]['acceptance_policy']=dict(
            development_defaults=dict(acceptance_only_tickets=['SF-311']))
        self.tick(1000)
        self.assertIn('workers active',self.tick(1061)['status'])
        self.config['projects'][0]['acceptance_policy']={}
        self.model['_watchdog_dependency_hints']['p']=[]
        self.assertIn('workers active',self.tick(1122)['status'])


class SnapshotIntegrationTests(unittest.TestCase):
    def test_main_collects_only_local_hints_and_never_emits_raw_show_credentials(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config=dict(projects=[dict(id='p',root=str(root))],workers={'Codex':['true']},
                        recovery_watchdog=dict(enabled=True,projects=['p'],stall_seconds=60,
                                               cooldown_seconds=300,timeout_seconds=120))
            config_path=root/'config.json';config_path.write_text(json.dumps(config))
            tickets=[dict(key='SF-311',status='blocked'),dict(key='SF-295',status='done')]
            detail=dict(key='SF-311',project='p',status='blocked',agent='Codex',
                        session='claim-generation',token='PRIVATE_COORDINATION_TOKEN',
                        evidence='private diagnostic',spec=dict(depends_on=['SF-295']),remote={})
            model=dict(inventory={'p':dict(state='ok',breakdown=dict(blocked=1))},
                       workers=[dict(id='active',project='p')],attention=[])
            calls=[]
            def run(args,**kwargs):
                calls.append(args)
                if args==['fleet-status','--json']:value=model
                elif args==['ticket','--project','p','list']:value=tickets
                elif args==['ticket','--project','p','show','SF-311']:
                    self.assertLessEqual(kwargs['timeout'],1);value=detail
                else:self.fail('unexpected nonlocal command '+repr(args))
                return subprocess.CompletedProcess(args,0,json.dumps(value),'')
            output=io.StringIO()
            with patch.object(sys,'argv',['watchdog','--config',str(config_path),'--state',str(root/'state')]), \
                 patch.object(w.subprocess,'run',side_effect=run), \
                 patch.dict(os.environ,{},clear=True), redirect_stdout(output):
                w.main()
            saved=json.loads((root/'state/recovery-watchdog/state.json').read_text())
            self.assertIn('SF-311',saved['observations']['p']['dependency_waits'])
            self.assertEqual(len(calls),3)
            self.assertNotIn('PRIVATE_COORDINATION_TOKEN',output.getvalue()+json.dumps(saved))
            self.assertNotIn('private diagnostic',output.getvalue()+json.dumps(saved))
