import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from test_streaming_source_reviews import c

class LegacyGitHubPause(unittest.TestCase):
    def test_live_legacy_pause_survives_coordinator_activation(self):
        with tempfile.TemporaryDirectory() as d:
            engine=c.Coordinator({'workers':{'Codex':['true'],'Claude':['true']},'projects':[]},Path(d))
            project={'id':'p','github_account':'account'}
            engine.store.db.execute('CREATE TABLE github_backoff(account TEXT PRIMARY KEY,retry_after REAL)')
            engine.store.db.execute('INSERT INTO github_backoff VALUES(?,?)',('account',500))
            with patch.object(c,'run',return_value='fixture-token') as command,patch.object(c,'shared_github_snapshot',return_value='{}') as transport:
                with patch.object(c.time,'time',return_value=100):
                    with self.assertRaisesRegex(RuntimeError,'Legacy GitHub cooldown'):c.Coordinator.gh(engine,project,['api','rate_limit'])
                    command.assert_not_called();transport.assert_not_called()
                with patch.object(c.time,'time',return_value=501):
                    self.assertEqual(c.Coordinator.gh(engine,project,['api','rate_limit']),'{}')
