"""Offline failure checks; fixtures here are not live recording evidence."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('encode_guide', Path(__file__).resolve().parents[1] / 'guide/encode_guide.py')
encode = importlib.util.module_from_spec(spec)
spec.loader.exec_module(encode)


class RecordingTest(unittest.TestCase):
    def test_encoder_bounds_waits_and_preserves_final_frame(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '00001.jpg').touch()
            (root / '00002.jpg').touch()
            (root / 'frames.json').write_text(json.dumps([
                {'file': '00001.jpg', 't': 1}, {'file': '00002.jpg', 't': 300}]))
            self.assertAlmostEqual(encode.concat_list(root), encode.MAX_HOLD + encode.LAST_HOLD)
            text = (root / 'list.ffconcat').read_text()
            self.assertEqual(text.count('file 00002.jpg'), 2)
            self.assertNotIn('299', text)

    def test_bad_frames_rejected_before_ffmpeg(self):
        for frames in [[], [{'file': '../secret.jpg', 't': 1}],
                       [{'file': '00001.jpg', 't': float('nan')}],
                       [{'file': '00001.jpg', 't': 2}, {'file': '00001.jpg', 't': 1}],
                       [{'file': '99999.jpg', 't': 1}]]:
            with self.subTest(frames=frames), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / '00001.jpg').touch()
                (root / 'frames.json').write_text(json.dumps(frames))
                with self.assertRaises(ValueError):
                    encode.concat_list(root)
                self.assertFalse((root / 'list.ffconcat').exists())

    def test_recorder_refuses_mutation_without_lease(self):
        from types import SimpleNamespace
        from unittest import mock
        spec = importlib.util.spec_from_file_location('record_guide', Path(__file__).resolve().parents[1] / 'guide/record_guide.py')
        recorder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(recorder)
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict('os.environ', {}, clear=True), mock.patch.object(recorder.Live, 'preflight') as preflight:
            with self.assertRaisesRegex(RuntimeError, 'lease required'):
                recorder.run(SimpleNamespace(runtime_dir=temp, wallet='/nonexistent/client.yaml', max_spend_mist=250000000, evidence=None, check=False))
            preflight.assert_not_called()

    def test_recorder_preserves_prior_runtime(self):
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location('record_guide', Path(__file__).resolve().parents[1] / 'guide/record_guide.py')
        recorder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(recorder)
        with tempfile.TemporaryDirectory() as temp:
            evidence = Path(temp) / 'evidence.json'
            evidence.write_text('preserve this unresolved run')
            with self.assertRaisesRegex(RuntimeError, 'must be empty'):
                recorder.run(SimpleNamespace(runtime_dir=temp))
            self.assertEqual(evidence.read_text(), 'preserve this unresolved run')

    def test_resume_preserves_non_replenishing_allowance(self):
        from types import SimpleNamespace
        from unittest import mock
        spec = importlib.util.spec_from_file_location('record_guide', Path(__file__).resolve().parents[1] / 'guide/record_guide.py')
        recorder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(recorder)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'evidence.json'
            original = dict(chain='4c78adac', operator='demo', max_spend_mist=250000000,
                            allowance_used_mist=153000000, start_balance_mist=500000000,
                            transactions=[])
            path.write_text(json.dumps(original))
            live = recorder.Live('/nonexistent/client.yaml', 'testnet', 250000000)
            live.chain, live.operator, live.start_balance = '4c78adac', 'demo', 400000000
            live.signer = lambda config, address: lambda raw: 'offline-signature'
            args = SimpleNamespace(runtime_dir=temp, resume=True, faucet=False, wallet='/nonexistent/client.yaml',
                                   max_spend_mist=250000000, evidence=str(path), check=False, state=None)
            with mock.patch.dict('os.environ', {'SUIFLEET_TESTNET_LOCK_TOKEN': 'offline-fixture'}), mock.patch.object(recorder, 'Live', return_value=live), mock.patch.object(recorder, 'assert_lease'), mock.patch.object(live, 'preflight'), mock.patch.object(recorder, 'load_state', side_effect=LookupError('stop before fixture reads')):
                with self.assertRaises(LookupError):
                    recorder.run(args)
                self.assertEqual(live.ledger.used, 153000000)
                self.assertEqual(live.ledger.remaining, 97000000)
                self.assertEqual(live.start_balance, 500000000)
                # An in-flight full reservation must replace the old settled
                # usage in the durable snapshot before any signature is returned.
                live.ledger.used = 232000000
                with mock.patch.object(live, 'decode', return_value={'digest': 'offline-digest'}):
                    self.assertEqual(live.signer(None, 'demo')('unsigned-fixture'), 'offline-signature')
                snapshot = json.loads(path.read_text())
                self.assertEqual(snapshot['allowance_used_mist'], 232000000)
                self.assertEqual(snapshot['signed_transactions'][-1]['digest'], 'offline-digest')
                args.max_spend_mist += 1
                with self.assertRaisesRegex(RuntimeError, 'allowance differs'):
                    recorder.run(args)

    def test_lease_failure_does_not_expose_token_in_traceback(self):
        import subprocess
        import traceback
        from types import SimpleNamespace
        from unittest import mock
        spec = importlib.util.spec_from_file_location('record_guide', Path(__file__).resolve().parents[1] / 'guide/record_guide.py')
        recorder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(recorder)
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict('os.environ', {'SUIFLEET_TESTNET_LOCK_TOKEN': 'private-fixture-token'}), mock.patch.object(recorder.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['ticket', '--lock-token', 'private-fixture-token'])):
            try:
                recorder.run(SimpleNamespace(runtime_dir=temp, wallet='/nonexistent/client.yaml', max_spend_mist=250000000, evidence=None, check=False))
            except RuntimeError as error:
                self.assertEqual(str(error), 'testnet mutation lease refused')
                self.assertNotIn('private-fixture-token', ''.join(traceback.format_exception(type(error), error, error.__traceback__)))
            else:
                self.fail('invalid lease must stop the run')
