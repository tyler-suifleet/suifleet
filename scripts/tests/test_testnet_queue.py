"""Keyless eligibility cannot occupy the shared signing-wallet queue."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class TestnetQueueTests(unittest.TestCase):
    def invoke(self, conclusion='success', queue=True, stale=False):
        with tempfile.TemporaryDirectory() as tmp:
            event, output = Path(tmp)/'event.json', Path(tmp)/'output'
            event.write_text(json.dumps({'workflow_run': {'id': 7}}))
            env = dict(GITHUB_REPOSITORY='owner/repo', GITHUB_EVENT_NAME='workflow_run',
                       GITHUB_EVENT_PATH=str(event), GITHUB_OUTPUT=str(output))
            calls = []
            def gh(command, **kwargs):
                path = command[2]
                calls.append(path)
                facts = {
                    'repos/owner/repo/commits/main': {'sha': 'b'*40},
                    'repos/owner/repo/actions/runs/7': dict(conclusion=conclusion,status='completed',workflow_id=3,
                        repository={'full_name':'owner/repo'},event='pull_request',head_sha='a'*40,actor={'login':'writer'}),
                    'repos/owner/repo/actions/workflows/3': {'path':'.github/workflows/ci.yml'},
                    'repos/owner/repo/collaborators/writer/permission': {'permission':'write'},
                    'repos/owner/repo/commits/'+('a'*40)+'/pulls': [{'number':1}],
                    'repos/owner/repo/pulls/1': dict(number=1,state='open',user={'login':'writer'},
                        base={'ref':'main','repo':{'full_name':'owner/repo'}},
                        head={'sha': ('c' if stale else 'a')*40,'repo':{'full_name':'owner/repo'}}),
                }
                if '/environments/' in path:
                    raise RuntimeError('protected environment reached')
                return type('Result',(),{'returncode':0,'stdout':json.dumps(facts[path])})()
            with patch.dict(os.environ,env), patch.object(sys,'argv',['testnet_ci.py']+(['--queue-plan'] if queue else [])), patch('subprocess.run',side_effect=gh):
                runpy.run_path(str(ROOT/'scripts/testnet_ci.py'),run_name='__main__')
            return output.read_text(),calls

    def test_current_writer_head_is_selected_without_environment_access(self):
        output,calls = self.invoke()
        self.assertIn('run=true',output)
        self.assertIn('head='+'a'*40,output)
        self.assertTrue(any('/permission' in x for x in calls))
        self.assertFalse(any('/environments/' in x for x in calls))

    def test_cancelled_failed_and_superseded_origins_do_not_queue(self):
        for conclusion in ('cancelled','failure','timed_out'):
            self.assertIn('run=false',self.invoke(conclusion)[0])
        self.assertIn('run=false',self.invoke(stale=True)[0])

    def test_protected_validation_still_independently_requires_environment(self):
        with self.assertRaisesRegex(RuntimeError,'protected environment'):
            self.invoke(queue=False)

    def test_workflow_separates_keyless_plan_from_serialized_wallet_jobs(self):
        workflow = (ROOT/'.github/workflows/testnet.yml').read_text()
        header,jobs = workflow.split('\njobs:\n',1)
        queue,rest = jobs.split('\n  prepare:\n',1)
        prepare,rest = rest.split('\n  validate:\n',1)
        validate,prune = rest.split('\n  prune-fixture-history:\n',1)
        self.assertNotIn('concurrency:',header)
        for forbidden in ('environment:','secrets.','concurrency:','statuses:','--status','--recheck','head_sha'):
            self.assertNotIn(forbidden,queue)
        self.assertIn("github.event.workflow_run.conclusion == 'success'",queue)
        self.assertIn('ref: ${{ github.workflow_sha }}',queue)
        self.assertIn('--queue-plan',queue)
        self.assertIn('needs: queue-plan',prepare)
        self.assertIn("needs.queue-plan.outputs.run == 'true'",prepare)
        self.assertIn('needs: prepare',validate)
        for protected in (validate,prune):
            self.assertIn('group: ${{ needs.prepare.outputs.concurrency }}',protected)
            self.assertIn('cancel-in-progress: false',protected)
            self.assertIn('queue: max',protected)
        self.assertIn('environment: live-package-upgrade',validate)
        self.assertIn('run: python3 scripts/testnet_ci.py\n',validate)
        self.assertIn('run: python3 "$RUNNER_TEMP/testnet_ci.py" --recheck',validate)
        self.assertNotIn('--queue-plan',validate)


if __name__ == '__main__': unittest.main()
