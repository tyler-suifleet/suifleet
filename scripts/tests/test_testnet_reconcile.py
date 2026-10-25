"""SF-146: uncertain testnet setup reconciliation, whole-run funding preflight
and classified exact-head status. Offline; no network, signing or spending."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sui_toolchain as tc
import testnet_reconcile as rc
import testnet_ci
import live_package_upgrade as live
from test_testnet_toolchain import BUILDER, BUILT, DEPS, MODULES, ROOT, SENDER

HAVE_SDK = (ROOT / 'dapp/node_modules/@mysten/sui').exists()
OPERATOR = tc.normal(SENDER)
OTHER = tc.normal('0x' + '0f' * 32)
PACKAGE = tc.normal('0x' + 'ab' * 32)
REGISTRY = tc.normal('0x' + 'ee' * 32)
CAP = tc.normal('0x' + 'ca' * 32)

PTB_BUILDER = r'''
const path = require('node:path');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const req = createRequire(path.resolve('dapp/package.json'));
(async () => {
  const spec = JSON.parse(process.argv[1]);
  const { Transaction } = await import(pathToFileURL(req.resolve('@mysten/sui/transactions')));
  const tx = new Transaction();
  tx.setSender(spec.sender); tx.setGasBudget(spec.budget); tx.setGasPrice(1000);
  tx.setGasPayment([]); tx.setExpiration({ ValidDuring: { minEpoch: '7', maxEpoch: '8', minTimestamp: null,
    maxTimestamp: null, chain: '69WiPg3DAQiwdxfncX6wYQ2siKwAe6L9BZthQea3JNMD', nonce: 5 } });
  for (const target of spec.targets) tx.moveCall({ target, arguments: [] });
  process.stdout.write(Buffer.from(await tx.build()).toString('base64'));
})().catch((e) => { console.error(e); process.exit(1); });
'''


def build(script, **spec):
    return subprocess.run(['node', '-e', script, json.dumps(spec)], cwd=ROOT, capture_output=True,
                          text=True, check=True, timeout=120).stdout


def facts(digest='D1', success=True, sender=OPERATOR, commands=None, created=None):
    return {'found': True, 'digest': digest, 'success': success, 'error': None if success else '"MoveAbort"',
            'epoch': '7', 'checkpoint': '9', 'sender': sender, 'gas_owner': sender, 'gas_budget': '400000000',
            'gas_used': {'computationCost': '3000000', 'storageCost': '338000000', 'storageRebate': '0'},
            'commands': commands if commands is not None else
            [{'kind': 'Publish', 'modules': MODULES, 'dependencies': DEPS}, {'kind': 'TransferObjects'}],
            'created': created if created is not None else [
                {'objectId': PACKAGE, 'type': 'package'},
                {'objectId': REGISTRY, 'type': PACKAGE + '::device_registry::DeviceRegistry'},
                {'objectId': CAP, 'type': '0x2::package::UpgradeCap'}]}


class FakeChain:
    def __init__(self, epoch=7, transactions=None, caps=(), coins=None, versions=None):
        self.current, self.transactions = epoch, dict(transactions or {})
        self.owned_caps, self.owned_coins, self.current_versions = list(caps), dict(coins or {}), dict(versions or {})
        self.reads = []

    def transaction(self, digest):
        self.reads.append(('transaction', digest))
        return self.transactions.get(digest)

    def epoch(self):
        return self.current

    def caps(self, owner):
        return self.owned_caps

    def coins(self, owner):
        return dict(self.owned_coins)

    def versions(self, ids):
        return {i: self.current_versions.get(i) for i in ids}


def pending(tx=True, created=1000.0, max_epoch='8', payment=()):
    marker = {'label': 'publish base', 'created': created, 'tx': None}
    if tx:
        marker['tx'] = {'tx_bytes': 'BYTES', 'digest': 'D1', 'sender': OPERATOR, 'gas_budget': 400_000_000,
                        'gas_payment': list(payment), 'expiration': {'kind': 'ValidDuring', 'max_epoch': max_epoch}}
    return marker


def decoded(digest='D1', sender=OPERATOR, max_epoch='8', payment=(), budget='400000000'):
    return lambda tx_bytes: {'digest': digest, 'sender': sender, 'gas_owner': sender, 'gas_budget': budget,
                             'gas_payment': [dict(p, digest='X') for p in payment],
                             'expiration': {'kind': 'ValidDuring', 'min_epoch': None, 'max_epoch': max_epoch}}


class Reconcile(unittest.TestCase):
    def decide(self, marker, chain, *, inspect=None, verify=None, resubmit=None, now=2000.0, legacy=None):
        resubmit = resubmit or Mock(side_effect=AssertionError('must not resubmit'))
        return rc.reconcile(marker, chain=chain, operator=OPERATOR, inspect=inspect or decoded(),
                            verify=verify or (lambda data, tx: None), resubmit=resubmit, now=now, legacy=legacy)

    def test_unknown_but_executed_publish_is_adopted_from_effects_without_resubmission(self):
        outcome, result = self.decide(pending(), FakeChain(transactions={'D1': facts()}))
        self.assertEqual(outcome, 'executed')
        self.assertEqual(next(c['packageId'] for c in result['objectChanges'] if c['type'] == 'published'), PACKAGE)
        self.assertEqual([c['objectId'] for c in result['objectChanges'] if c['type'] == 'created'], [REGISTRY, CAP])
        self.assertEqual(result['gas_mist'], 341_000_000)
        self.assertTrue(result['reconciled'])

    def test_timeout_before_execution_is_absent_once_its_window_expired(self):
        chain = FakeChain(epoch=9)
        outcome, reason = self.decide(pending(), chain)
        self.assertEqual(outcome, 'absent')
        self.assertIn('never executed and never can', reason)
        self.assertEqual(chain.reads, [('transaction', 'D1'), ('transaction', 'D1')])  # Re-checked after deciding.

    def test_coin_paid_transaction_is_dead_once_its_gas_coin_moved(self):
        coin = tc.normal('0x' + 'c0' * 32)
        payment = [{'objectId': coin, 'version': '5'}, {'objectId': tc.normal('0x' + 'c1' * 32), 'version': '0'}]
        outcome, reason = self.decide(pending(max_epoch=None, payment=payment), FakeChain(versions={coin: '6'}),
                                      inspect=decoded(max_epoch=None, payment=payment))
        self.assertEqual(outcome, 'absent')
        self.assertIn('moved past version 5', reason)

    def test_still_executable_transaction_resubmits_identical_bytes_once(self):
        sent = Mock(return_value={'digest': 'D1', 'effects': {'status': {'status': 'success'}}, 'objectChanges': []})
        outcome, result = self.decide(pending(), FakeChain(epoch=8), resubmit=sent)
        self.assertEqual(outcome, 'executed')
        sent.assert_called_once()
        self.assertEqual(sent.call_args.args[0]['tx_bytes'], 'BYTES')

    def test_failed_effects_are_known_and_clear_without_adoption(self):
        outcome, result = self.decide(pending(), FakeChain(transactions={'D1': facts(success=False)}))
        self.assertEqual(outcome, 'failed')
        self.assertEqual(result['effects']['status']['status'], 'failure')

    def test_tampered_provenance_or_foreign_effects_are_retained_as_uncertain(self):
        for inspect in (decoded(digest='OTHER'), decoded(sender=OTHER)):
            with self.assertRaisesRegex(rc.Uncertain, 'tampered'):
                self.decide(pending(), FakeChain(transactions={'D1': facts()}), inspect=inspect)
        def refuse(data, tx):
            raise tc.NotSubmitted('transaction bytecode differs from the pinned release compiler output')
        with self.assertRaisesRegex(rc.Uncertain, 'verified intent'):
            self.decide(pending(), FakeChain(transactions={'D1': facts()}), verify=refuse)
        with self.assertRaisesRegex(rc.Uncertain, 'not sent and paid'):
            self.decide(pending(), FakeChain(transactions={'D1': facts(sender=OTHER)}))

    def test_tampered_death_proof_fields_retain_the_marker_without_any_submission(self):
        coin = tc.normal('0x' + 'c0' * 32)
        signed = [{'objectId': coin, 'version': '5'}]
        cases = [
            # Cached window ends before the signed one: epoch 7 would wrongly look expired.
            (pending(max_epoch='6'), decoded(max_epoch='8'), FakeChain(epoch=7), 'expiration'),
            # Cached coin reference that has moved, while the signed coin is untouched.
            (pending(payment=[{'objectId': coin, 'version': '4'}]), decoded(payment=signed),
             FakeChain(epoch=7, versions={coin: '5'}), 'gas_payment'),
            # Cached budget below the signed one would under-reserve the allowance.
            (pending(), decoded(budget='900000000'), FakeChain(epoch=7), 'gas_budget'),
        ]
        for marker, inspect, chain, field in cases:
            with self.subTest(field=field):
                with self.assertRaisesRegex(rc.Uncertain, f'{field}.*tampered'):
                    self.decide(marker, chain, inspect=inspect)
                self.assertEqual(chain.reads, [])  # Decided before any chain lookup or resubmission.
        with self.assertRaisesRegex(rc.Uncertain, 'decodable identity'):
            self.decide(pending(), FakeChain(), inspect=lambda b: {'digest': 'D1', 'sender': OPERATOR})

    def test_interrupted_before_signing_and_beyond_history_window(self):
        self.assertEqual(self.decide(pending(tx=False), FakeChain())[0], 'absent')
        with self.assertRaisesRegex(rc.Uncertain, 'history window'):
            self.decide(pending(created=0.0), FakeChain(epoch=9), now=rc.RETENTION_SECONDS + 1)
        with self.assertRaisesRegex(rc.Uncertain, 'malformed'):
            rc.normal_pending({'tx': None})

    def test_missing_historical_identity_requires_explicit_authenticated_path(self):
        with self.assertRaisesRegex(rc.Uncertain, 'explicit reconciliation'):
            self.decide('publish base', FakeChain())
        marker = rc.normal_pending('register device + issue cap (base)')
        with self.assertRaisesRegex(rc.Uncertain, 'explicit reconciliation'):
            rc.legacy_publication(marker, chain=FakeChain(), operator=OPERATOR, built=BUILT, known=set(), now=1)


class LegacyPublication(unittest.TestCase):
    def cap(self, object_id=CAP, digest='D1', version='1', package=PACKAGE):
        return {'objectId': object_id, 'version': '1', 'package': package, 'cap_version': version,
                'previous_transaction': digest}

    def legacy(self, chain, marker, known=(), now=100.0):
        return rc.legacy_publication(marker, chain=chain, operator=OPERATOR, built=BUILT, known=set(known), now=now)

    def test_executed_historical_publish_is_adopted_from_exact_bytecode(self):
        marker = rc.normal_pending('publish base')
        known = tc.normal('0x' + 'aa' * 32)
        chain = FakeChain(transactions={'D1': facts(), 'D0': facts(digest='D0')},
                          caps=[self.cap(object_id=known, digest='D0'), self.cap()])
        outcome, result = self.legacy(chain, marker, known={known})
        self.assertEqual(outcome, 'executed')
        self.assertEqual(result['digest'], 'D1')
        self.assertEqual(marker['observed']['epoch'], 7)

    def test_absence_waits_for_expiry_and_moved_coins_without_dropping_the_marker(self):
        coin = tc.normal('0x' + 'c0' * 32)
        other = facts(commands=[{'kind': 'Publish', 'modules': MODULES[:1], 'dependencies': DEPS},
                                {'kind': 'TransferObjects'}])
        chain = FakeChain(transactions={'D1': other}, caps=[self.cap()], coins={coin: '3'}, versions={coin: '3'})
        marker = rc.normal_pending('publish base')
        for epoch in (7, 8):
            chain.current = epoch
            with self.assertRaisesRegex(rc.Uncertain, 'could still execute through epoch 8'):
                self.legacy(chain, marker)
            self.assertEqual(marker['observed']['epoch'], 7)  # First observation is the bound.
        chain.current = 9
        with self.assertRaisesRegex(rc.Uncertain, 'gas coins observed'):
            self.legacy(chain, marker)
        chain.current_versions[coin] = '4'
        outcome, reason = self.legacy(chain, marker)
        self.assertEqual(outcome, 'absent')
        self.assertIn('expired', reason)

    def test_ambiguous_or_unauthenticated_unrecorded_caps_are_refused(self):
        chain = FakeChain(transactions={'D1': facts(), 'D2': facts(digest='D2')},
                          caps=[self.cap(), self.cap(object_id=tc.normal('0x' + 'cb' * 32), digest='D2')])
        with self.assertRaisesRegex(rc.Uncertain, '2 unrecorded publications'):
            self.legacy(chain, rc.normal_pending('publish base'))
        with self.assertRaisesRegex(rc.Uncertain, 'without retrievable creation history'):
            self.legacy(FakeChain(caps=[self.cap(digest='PRUNED')]), rc.normal_pending('publish base'))

    def test_historical_upgrade_inspects_the_publication_cap(self):
        head = tc.normal('0x' + 'ad' * 32)
        upgrade = facts(digest='U1', commands=[{'kind': 'MoveCall'}, {'kind': 'Upgrade', 'modules': MODULES,
                        'dependencies': DEPS, 'package': PACKAGE}, {'kind': 'MoveCall'}],
                        created=[{'objectId': head, 'type': 'package'}])
        target = {'cap': CAP, 'package': PACKAGE}
        chain = FakeChain(transactions={'U1': upgrade}, caps=[self.cap(digest='U1', version='2', package=head)])
        outcome, result = rc.legacy_publication(rc.normal_pending('upgrade to head'), chain=chain, operator=OPERATOR,
                                                built=BUILT, known=set(), now=1, upgrade=target)
        self.assertEqual((outcome, result['objectChanges'][0]['packageId']), ('executed', head))
        chain.owned_caps = [self.cap(digest='U1', version='2', package=tc.normal('0x' + 'bd' * 32))]
        with self.assertRaisesRegex(rc.Uncertain, 'unverified transaction'):
            rc.legacy_publication(rc.normal_pending('upgrade to head'), chain=chain, operator=OPERATOR,
                                  built=BUILT, known=set(), now=1, upgrade=target)

    def test_known_objects_cover_every_state_record(self):
        state = {'fixtures': {'k': {'upgrade_cap': CAP.upper().replace('0X', '0x')}},
                 'security': {'nested': [PACKAGE]}}
        self.assertEqual(rc.known_objects(state), {CAP, PACKAGE})


@unittest.skipUnless(HAVE_SDK, 'needs dapp/node_modules (make dapp-install)')
class PersistBeforeSigning(unittest.TestCase):
    def test_identity_is_durable_before_serialized_submission(self):
        tx_bytes = build(BUILDER, sender=SENDER, budget=400_000_000, modules=MODULES, dependencies=DEPS)
        with tempfile.TemporaryDirectory() as root:
            package = Path(root)
            (package / 'Move.lock').write_text('pinned\n')
            compiled = SimpleNamespace(returncode=0, stdout=json.dumps(BUILT), stderr='')
            order = []

            def client(*command, json_output=True, check=True):
                order.append(command[0])
                if command[0] == 'serialized-tx':
                    return {'effects': {}}
                return SimpleNamespace(returncode=0, stdout=tx_bytes + '\n', stderr='')

            def run(command, **kwargs):
                return compiled if command[1] == 'move' else subprocess.run(command, **kwargs)
            persisted = []
            tc.package_submitter(client, '/b/sui', package, [], sender=SENDER, run=run,
                                 persist=lambda identity: (order.append('persist'), persisted.append(identity)))(400_000_000)
            self.assertEqual(order, ['test-publish', 'persist', 'serialized-tx'])
            identity = persisted[0]
            self.assertEqual(identity['tx_bytes'], tx_bytes)
            self.assertEqual(identity['digest'], tc.decode(tx_bytes)['digest'])
            self.assertEqual((identity['sender'], identity['gas_budget']), (OPERATOR, 400_000_000))
            self.assertEqual(len(identity['gas_payment']), 1)
            order.clear()

            def broken(identity):
                raise OSError('disk full')
            with self.assertRaisesRegex(tc.NotSubmitted, 'not persisted'):
                tc.package_submitter(client, '/b/sui', package, [], sender=SENDER, run=run, persist=broken)(400_000_000)
            self.assertNotIn('serialized-tx', order)

    def test_setup_ptb_is_verified_persisted_then_submitted(self):
        targets = [f'{PACKAGE}::device_group::add_device']
        tx_bytes = build(PTB_BUILDER, sender=SENDER, budget=200_000_000, targets=targets)
        order = []

        def client(*command, json_output=True, check=True):
            order.append(command[0] if command[0] != 'ptb' else ('ptb', '--serialize-unsigned-transaction' in command))
            if command[0] == 'serialized-tx':
                return {'effects': {}}
            return SimpleNamespace(returncode=0, stdout=tx_bytes + '\n', stderr='')
        persisted = []
        tc.ptb_submitter(client, ['--move-call', targets[0]], sender=SENDER, calls=targets,
                         persist=persisted.append)(200_000_000)
        self.assertEqual(order, [('ptb', True), 'serialized-tx'])
        self.assertEqual(persisted[0]['expiration'], {'kind': 'ValidDuring', 'max_epoch': '8'})
        self.assertEqual(persisted[0]['gas_payment'], [])
        order.clear()
        for calls, budget in ((['0x2::coin::zero'], 200_000_000), (targets, 10_000_000)):
            with self.assertRaises(tc.NotSubmitted):
                tc.ptb_submitter(client, [], sender=SENDER, calls=calls, persist=persisted.append)(budget)
        with self.assertRaises(tc.NotSubmitted):
            tc.ptb_submitter(client, [], sender=OTHER, calls=targets)(200_000_000)
        self.assertNotIn('serialized-tx', order)


class Funding(unittest.TestCase):
    def test_full_sequence_and_single_gas_pool_requirements(self):
        self.assertIsNone(live.funding_shortfall((1_700_000_000, 0), 1_650_000_000))
        self.assertIn('may spend up to 1650000000', live.funding_shortfall((1_640_908_569, 0), 1_650_000_000))
        self.assertIn('no single gas pool', live.funding_shortfall((300_000_000, 300_000_000), 600_000_000))
        self.assertEqual(live.gas_pools({'addressMistBalance': '5', 'gasCoins': [{'mistBalance': '2'}]}), (5, 2))
        self.assertEqual(live.gas_pools([{'mistBalance': '2'}]), (0, 2))

    def test_failure_classes(self):
        self.assertEqual(live.failure_class(live.FundingShortfall('x')), 'funding')
        self.assertEqual(live.failure_class(live.Spent('x')), 'funding')
        self.assertEqual(live.failure_class(rc.Uncertain('x')), 'uncertain')
        self.assertEqual(live.failure_class(RuntimeError("code: 'Deadline expired before operation could complete'")),
                         'infrastructure')
        self.assertEqual(live.failure_class(AssertionError('receipt never became 1')), 'source')

    def test_ci_controller_requires_every_selected_suite_up_front(self):
        from ci_testnet_wallet import execute
        for relevant, required in ((True, '1650000000'), (False, '800000000')):
            with tempfile.TemporaryDirectory() as root:
                calls = []

                def child(command, check):
                    calls.append(command)
                    return SimpleNamespace(returncode=0)
                self.assertEqual(execute(root, str(Path(root) / 'state.json'), 'a' * 40, b'k', child,
                                         lambda base: relevant), 0)
                first = calls[0]
                self.assertEqual(first[first.index('--required-balance-mist') + 1], required)
                self.assertEqual(first[first.index('--max-spend-mist') + 1], '800000000')
                self.assertEqual(len(calls), 3 if relevant else 1)


ADDRESS = '0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac'
HEAD = tc.normal('0x' + 'ad' * 32)
DEVICE = tc.normal('0x' + 'd0' * 32)


class RunFlow(unittest.TestCase):
    """Drive run() up to its first chain verification with an offline CLI."""

    def harness(self, root, state, *, balance='2000000000', serialized=None, faucet=None, required=None, upgrade=False):
        tools = {'build': {'path': '/compiler', 'version': tc.BUILD['version']},
                 'network': {'path': '/network', 'version': tc.NETWORK['version'], 'sha256': 'pinned'}}
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as archive:
            info = tarfile.TarInfo('contracts/Move.lock'); data = b'offline fixture'
            info.size = len(data); archive.addfile(info, io.BytesIO(data))
        real_run = subprocess.run
        commands = []

        def command(argv, **kwargs):
            if argv[0] == 'tar':
                return real_run(argv, **kwargs)
            if argv[0] == 'git':
                return SimpleNamespace(stdout=buffer.getvalue(), returncode=0)
            commands.append(argv)
            if 'envs' in argv:
                output = json.dumps([[{'alias': 'testnet', 'rpc': 'https://offline.invalid'}], 'testnet'])
            elif 'active-address' in argv:
                output = ADDRESS
            elif 'faucet' in argv:
                return faucet(argv)
            elif 'serialized-tx' in argv:
                output = json.dumps(serialized(argv))
            else:
                self.assertIn('gas', argv)
                output = json.dumps({'addressMistBalance': balance[0] if isinstance(balance, list) else balance,
                                     'gasCoins': []})
                if isinstance(balance, list) and len(balance) > 1:
                    balance.pop(0)
            return SimpleNamespace(stdout=output, stderr='', returncode=0)
        digest = 'bytecode-v1:' + bytes(range(32)).hex()
        head_digest = 'bytecode-v1:' + bytes(range(1, 33)).hex() if upgrade else digest
        key = live.fixture_key(digest, head_digest)
        compiled = [dict(BUILT, digest=list(range(32))), dict(BUILT, digest=list(range(1, 33)) if upgrade else list(range(32)))]
        live.save_state(root / 'state.json', state(key, digest, tools))
        args = SimpleNamespace(wallet=str(root / 'client.yaml'), network='testnet', evidence=str(root / 'evidence.json'),
                               max_spend_mist=800000000, required_balance_mist=required, build_sui='/compiler',
                               network_sui='/network', faucet_tool='/network' if faucet else None, base_ref='b' * 40,
                               walrus='simulated', state=str(root / 'state.json'), fresh=False)
        walrus = SimpleNamespace(aggregator='http://offline.invalid', mode='simulated', close=Mock())
        patches = [patch.object(live.subprocess, 'run', side_effect=command),
                   patch.object(live.subprocess, 'check_output', return_value='b' * 40),
                   patch.object(live.toolchain, 'check_tools', return_value=tools),
                   patch.object(live.toolchain, 'resolve', side_effect=lambda tool: tool),
                   patch.object(live.toolchain, 'preflight', return_value={'protocol_version': 139}),
                   patch.object(live.toolchain, 'compile_package', side_effect=compiled),
                   patch.object(live.toolchain, 'framework_pins', return_value={}),
                   patch.object(live.toolchain, 'package_submitter', side_effect=AssertionError('no republication')),
                   patch.object(live.dm, 'chain_facts', return_value={'chain_identifier': '4c78adac'}),
                   patch.object(live.live_common, 'Walrus', return_value=walrus),
                   patch.dict('os.environ', {'SUIFLEET_TESTNET_LOCK_TOKEN': ''})]
        return args, key, patches, commands

    def run_flow(self, args, patches, extra=()):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for item in [*patches, *extra]:
                stack.enter_context(item)
            live.run(args)

    def state_with(self, marker):
        def state(key, digest, tools):
            return {'chain': '4c78adac', 'operator': ADDRESS, 'fixtures': {}, 'publications': {key: {
                'source_pair': [digest, digest], 'compiler': tools['build']['version'], 'built': {},
                'pending_operation': marker}}}
        return state

    def test_restart_adopts_unknown_but_executed_publish_without_republishing(self):
        marker = pending()
        marker['tx']['sender'] = tc.normal(ADDRESS)
        chain = FakeChain(epoch=8, transactions={'D1': facts(sender=tc.normal(ADDRESS))})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, key, patches, _ = self.harness(root, self.state_with(marker))
            stop = RuntimeError('stop at publication verification')
            with self.assertRaisesRegex(RuntimeError, 'stop at publication verification'):
                self.run_flow(args, patches, [
                    patch.object(live.reconciler, 'Chain', return_value=chain),
                    patch.object(live.toolchain, 'decode', side_effect=decoded(sender=ADDRESS)),
                    patch.object(live.toolchain, 'verify_package_transaction', return_value={'kind': 'publish'}),
                    patch.object(live, 'verify_publication', side_effect=stop)])
            saved = json.loads((root / 'state.json').read_text())['publications'][key]
            self.assertNotIn('pending_operation', saved)
            self.assertEqual((saved['base_package'], saved['registry'], saved['upgrade_cap']), (PACKAGE, REGISTRY, CAP))
            self.assertEqual(saved['built']['base']['digest'], bytes(range(32)).hex())
            evidence = json.loads((root / 'evidence.json').read_text())
            self.assertEqual(evidence['reconciliations'][0]['outcome'], 'executed')
            self.assertEqual(evidence['allowance_used_mist'], 0)  # Charged by the run that submitted it.

    def test_restart_resubmits_identical_retained_bytes_through_the_ledger(self):
        marker = pending()
        marker['tx']['sender'] = tc.normal(ADDRESS)
        executed = {'digest': 'D1', 'effects': {'status': {'status': 'success'}, 'gasUsed': {
            'computationCost': '3000000', 'storageCost': '338000000', 'storageRebate': '0'}},
            'objectChanges': [{'type': 'published', 'packageId': PACKAGE},
                              {'type': 'created', 'objectType': PACKAGE + '::device_registry::DeviceRegistry', 'objectId': REGISTRY},
                              {'type': 'created', 'objectType': '0x2::package::UpgradeCap', 'objectId': CAP}]}
        sent = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, key, patches, _ = self.harness(root, self.state_with(marker),
                                                 serialized=lambda argv: (sent.append(argv[-2]), executed)[1])
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                self.run_flow(args, patches, [
                    patch.object(live.reconciler, 'Chain', return_value=FakeChain(epoch=8)),
                    patch.object(live.toolchain, 'decode', side_effect=decoded(sender=ADDRESS)),
                    patch.object(live.toolchain, 'verify_package_transaction', return_value={'kind': 'publish'}),
                    patch.object(live, 'verify_publication', side_effect=RuntimeError('stop'))])
            self.assertEqual(sent, ['BYTES'])
            saved = json.loads((root / 'state.json').read_text())['publications'][key]
            self.assertEqual(saved['base_package'], PACKAGE)
            evidence = json.loads((root / 'evidence.json').read_text())
            self.assertEqual(evidence['transactions'][0]['label'], 'publish base (retained bytes)')
            self.assertEqual(evidence['transactions'][0]['gas_mist'], 341_000_000)

    def test_historical_marker_is_retained_with_its_observation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, key, patches, _ = self.harness(root, self.state_with('publish base'))
            with self.assertRaisesRegex(rc.Uncertain, 'could still execute'):
                self.run_flow(args, patches, [patch.object(live.reconciler, 'Chain', return_value=FakeChain(epoch=1262))])
            saved = json.loads((root / 'state.json').read_text())['publications'][key]
            self.assertEqual(saved['pending_operation']['label'], 'publish base')
            self.assertTrue(saved['pending_operation']['legacy'])
            self.assertEqual(saved['pending_operation']['observed']['epoch'], 1262)

    def upgrade_state(self, marker):
        def state(key, digest, tools):
            return {'chain': '4c78adac', 'operator': ADDRESS, 'fixtures': {}, 'publications': {key: {
                'source_pair': key.rsplit('@', 1)[0].split('..'), 'compiler': tools['build']['version'],
                'built': {'base': {'kind': 'publish'}}, 'base_package': PACKAGE, 'registry': REGISTRY,
                'upgrade_cap': CAP, 'device_key': 'offline-device-key', 'device_address': DEVICE,
                'device': tc.normal('0x' + 'de' * 32), 'device_cap': tc.normal('0x' + 'dc' * 32),
                'group': tc.normal('0x' + 'd9' * 32), 'member_added': True, 'pending_operation': marker}}}
        return state

    def restart_after_executed_upgrade(self, marker, chain, decode=None, cap_package=HEAD, stop=('stop at activation', tc.NotSubmitted),
                                       cap_version=2, submitter=None):
        """Resume a checkpointed upgrade and stop before activation/signing.

        By default the upgrade succeeded and its cap already points at HEAD;
        the failed-upgrade case retains the original package and cap version.
        """
        reads = []

        def chain_facts(rpc, *ids):
            if len(ids) == 3:  # verify_publication: base package, UpgradeCap, registry.
                reads.append(ids)
                return {'chain_identifier': '4c78adac', 'objects': {CAP: {
                    'kind': 'object', 'type': '0x2::package::UpgradeCap', 'owner': {'address': ADDRESS},
                    'upgrade_cap': {'package': cap_package, 'version': cap_version}}}}
            return {'chain_identifier': '4c78adac'}

        def output(argv, **kwargs):
            return DEVICE if '--derive-address' in argv else 'b' * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, key, patches, commands = self.harness(root, self.upgrade_state(marker), upgrade=True)
            patches[1] = patch.object(live.subprocess, 'check_output', side_effect=output)
            extra = [patch.object(live.reconciler, 'Chain', return_value=chain),
                     patch.object(live.dm, 'chain_facts', side_effect=chain_facts),
                     patch.object(live, 'owner_of', side_effect=lambda client, oid: ADDRESS if oid == CAP else 'shared'),
                     patch.object(live.toolchain, 'write_pubfile'),
                     patch.object(live.toolchain, 'verify_package_transaction', return_value={'kind': 'upgrade'}),
                     patch.object(live.toolchain, 'ptb_submitter', side_effect=tc.NotSubmitted('stop at activation'))]
            if submitter:
                extra.append(patch.object(live.toolchain, 'package_submitter', side_effect=submitter))
            if decode:
                extra.append(patch.object(live.toolchain, 'decode', side_effect=decode))
            with self.assertRaisesRegex(stop[1], stop[0]):
                self.run_flow(args, patches, extra)
            saved = json.loads((root / 'state.json').read_text())['publications'][key]
            evidence = json.loads((root / 'evidence.json').read_text())
            return saved, evidence, commands, reads

    def test_restart_adopts_executed_but_uncheckpointed_upgrade_from_retained_bytes(self):
        marker = pending(max_epoch='8')
        marker['label'] = 'upgrade to head'
        marker['tx'].update(digest='U1', sender=tc.normal(ADDRESS))
        upgrade = facts(digest='U1', sender=tc.normal(ADDRESS), created=[{'objectId': HEAD, 'type': 'package'}],
                        commands=[{'kind': 'MoveCall'}, {'kind': 'Upgrade', 'modules': MODULES, 'dependencies': DEPS,
                                                         'package': PACKAGE}, {'kind': 'MoveCall'}])
        chain = FakeChain(epoch=8, transactions={'U1': upgrade})
        saved, evidence, commands, reads = self.restart_after_executed_upgrade(
            marker, chain, decode=decoded(digest='U1', sender=ADDRESS))
        self.assertNotIn('pending_operation', saved)  # Cleared atomically with the recorded head.
        self.assertEqual(saved['head_package'], HEAD)
        self.assertEqual(saved['base_package'], PACKAGE)
        self.assertEqual(len(reads), 2)  # Lenient pre-check, then strict check of the adopted head.
        self.assertEqual(evidence['reconciliations'][0]['outcome'], 'executed')
        self.assertEqual(evidence['reconciliations'][0]['digest'], 'U1')
        self.assertEqual(evidence['transactions'], [])
        self.assertFalse(any('serialized-tx' in c or 'ptb' in c for c in commands))  # No second upgrade.

    def test_failed_upgrade_is_reconciled_before_new_bounded_reservation(self):
        # Run 40185557156 exhausted the gas left after publishing the base.
        # A continuation must reuse that base, prove the old upgrade failed,
        # and reserve from the new run's unchanged ceiling before signing.
        marker = pending(max_epoch='8')
        marker['label'] = 'upgrade to head'
        marker['tx'].update(digest='U1', sender=tc.normal(ADDRESS))
        failed = facts(digest='U1', sender=tc.normal(ADDRESS), success=False)
        failed['error'] = 'InsufficientGas'
        failed['gas_used'] = {'computationCost': '4650000', 'storageCost': '1634000',
                              'storageRebate': '1633837'}
        chain = FakeChain(epoch=8, transactions={'U1': failed})
        reservations = []

        def submitter(*args, **kwargs):
            self.assertEqual(kwargs['upgrade'], {'cap': CAP, 'package': PACKAGE})

            def submit(budget):
                reservations.append(budget)
                raise tc.NotSubmitted('stop before signing recovered upgrade')
            return submit

        saved, evidence, commands, _ = self.restart_after_executed_upgrade(
            marker, chain, decode=decoded(digest='U1', sender=ADDRESS),
            cap_package=PACKAGE, cap_version=1, submitter=submitter,
            stop=('stop before signing recovered upgrade', tc.NotSubmitted))
        self.assertEqual(reservations, [800_000_000])
        self.assertEqual(saved['base_package'], PACKAGE)
        self.assertNotIn('head_package', saved)
        self.assertNotIn('pending_operation', saved)
        self.assertEqual(evidence['reconciliations'][0]['outcome'], 'failed')
        self.assertEqual(evidence['reconciliations'][0]['gas_mist'], 4_650_163)
        self.assertEqual(evidence['reconciliations'][0]['charged_by'], 'earlier run')
        self.assertEqual(evidence['transactions'][0]['status'], 'not_submitted')
        self.assertEqual(evidence['allowance_used_mist'], 0)
        self.assertFalse(any('serialized-tx' in c or 'ptb' in c for c in commands))

    def test_adopted_upgrade_must_be_the_package_the_cap_points_at(self):
        marker = pending(max_epoch='8')
        marker['label'] = 'upgrade to head'
        marker['tx'].update(digest='U1', sender=tc.normal(ADDRESS))
        upgrade = facts(digest='U1', sender=tc.normal(ADDRESS), created=[{'objectId': HEAD, 'type': 'package'}],
                        commands=[{'kind': 'MoveCall'}, {'kind': 'Upgrade', 'modules': MODULES, 'dependencies': DEPS,
                                                         'package': PACKAGE}, {'kind': 'MoveCall'}])
        saved, evidence, commands, reads = self.restart_after_executed_upgrade(
            marker, FakeChain(epoch=8, transactions={'U1': upgrade}), decode=decoded(digest='U1', sender=ADDRESS),
            cap_package=OTHER, stop=('', AssertionError))
        self.assertEqual(saved['pending_operation']['label'], 'upgrade to head')  # Retained on disk.
        self.assertNotIn('head_package', saved)
        self.assertEqual(len(reads), 2)
        self.assertFalse(any('serialized-tx' in c or 'ptb' in c for c in commands))

    def test_restart_adopts_executed_but_uncheckpointed_historical_upgrade(self):
        upgrade = facts(digest='U1', sender=tc.normal(ADDRESS), created=[{'objectId': HEAD, 'type': 'package'}],
                        commands=[{'kind': 'MoveCall'}, {'kind': 'Upgrade', 'modules': MODULES, 'dependencies': DEPS,
                                                         'package': PACKAGE}, {'kind': 'MoveCall'}])
        cap = {'objectId': CAP, 'version': '9', 'package': HEAD, 'cap_version': '2', 'previous_transaction': 'U1'}
        chain = FakeChain(epoch=8, transactions={'U1': upgrade}, caps=[cap])
        saved, evidence, commands, _ = self.restart_after_executed_upgrade('upgrade to head', chain)
        self.assertNotIn('pending_operation', saved)
        self.assertEqual(saved['head_package'], HEAD)
        self.assertTrue(evidence['reconciliations'][0]['legacy'])
        self.assertEqual(evidence['transactions'], [])
        self.assertFalse(any('serialized-tx' in c or 'ptb' in c for c in commands))

    def test_pending_upgrade_never_relaxes_an_unrelated_cap(self):
        publication = {'base_package': PACKAGE, 'registry': REGISTRY, 'upgrade_cap': CAP,
                       'pending_operation': 'upgrade to head'}

        def verify(package, version, publication=publication):
            fact = {'chain_identifier': '4c78adac', 'objects': {CAP: {
                'kind': 'object', 'type': '0x2::package::UpgradeCap', 'owner': {'address': ADDRESS},
                'upgrade_cap': {'package': package, 'version': version}}}}
            with patch.object(live.dm, 'chain_facts', return_value=fact), \
                    patch.object(live, 'owner_of', side_effect=lambda client, oid: ADDRESS if oid == CAP else 'shared'):
                live.verify_publication(publication, 'rpc', ADDRESS, None)
        verify(PACKAGE, 1)
        verify(HEAD, 2)  # Unrecorded upgrade: authenticated by the upgrade step.
        for package, version, other in ((PACKAGE, 2, publication), (HEAD, 3, publication),
                                         (HEAD, 2, dict(publication, pending_operation='add device to group (base)')),
                                         (HEAD, 2, {k: v for k, v in publication.items() if k != 'pending_operation'}),
                                         (OTHER, 2, dict(publication, head_package=HEAD))):
            with self.subTest(package=package, version=version, other=other.get('pending_operation')):
                with self.assertRaises(AssertionError):
                    verify(package, version, other)

    def test_budget_exhaustion_between_suites_is_refused_before_any_mutation(self):
        faucets = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, key, patches, commands = self.harness(
                root, self.state_with('publish base'), balance=['1640908569', '1640908569'],
                faucet=lambda argv: (faucets.append(argv), SimpleNamespace(returncode=1, stdout='', stderr=''))[1],
                required=1_650_000_000)
            chain = Mock(side_effect=AssertionError('no setup before funding'))
            with self.assertRaises(live.FundingShortfall):
                self.run_flow(args, patches, [patch.object(live.reconciler, 'Chain', chain)])
            self.assertEqual(len(faucets), 1)  # One bounded faucet attempt per run.
            evidence = json.loads((root / 'evidence.json').read_text())
            self.assertEqual(evidence['transactions'], [])
            self.assertEqual(evidence['funding']['required_mist'], 1_650_000_000)
            self.assertFalse(evidence['faucet']['success'])
            self.assertFalse(any('serialized-tx' in c or 'ptb' in c for c in commands))


class Quarantine(unittest.TestCase):
    """User-authorized one-time abandonment of one historical uncertain fixture."""
    INCIDENT = 'sf146-test-incident'

    def incident(self, key, operator=ADDRESS):
        return patch.dict(live.QUARANTINE_INCIDENTS, {self.INCIDENT: {
            'operator': operator, 'fixture_key': key, 'label': 'publish base', 'reserved_mist': 400_000_000,
            'source': {'run': 40095854988, 'epoch': 1262}}}, clear=True)

    def published(self):
        return {'digest': 'N1', 'effects': {'status': {'status': 'success'}, 'gasUsed': {
            'computationCost': '3000000', 'storageCost': '86000000', 'storageRebate': '0'}},
            'objectChanges': [{'type': 'published', 'packageId': HEAD},
                              {'type': 'created', 'objectType': HEAD + '::device_registry::DeviceRegistry', 'objectId': REGISTRY},
                              {'type': 'created', 'objectType': '0x2::package::UpgradeCap', 'objectId': CAP}]}

    def submitter(self, calls, persisted):
        def submitter(*args, persist, record, **kwargs):
            def command(budget):
                calls.append(budget)
                persisted.append(json.loads((self.root / 'state.json').read_text()))
                persist({'tx_bytes': 'NEW', 'digest': 'N1', 'sender': tc.normal(ADDRESS), 'gas_budget': budget,
                         'gas_payment': [], 'expiration': {'kind': 'ValidDuring', 'max_epoch': '1263'}})
                return self.published()
            return command
        return submitter

    def flow(self, state, *, incident=True, key_override=None, balance='2000000000'):
        flow = RunFlow()
        args, key, patches, commands = flow.harness(self.root, state, balance=balance)
        calls, persisted = [], []
        extra = [patch.object(live.reconciler, 'Chain', return_value=FakeChain(epoch=1262)),
                 patch.object(live.toolchain, 'package_submitter', side_effect=self.submitter(calls, persisted)),
                 patch.object(live, 'verify_publication', side_effect=RuntimeError('stop at publication verification'))]
        if incident:
            extra.append(self.incident(key_override or key))
        error = None
        try:
            flow.run_flow(args, patches, extra)
        except Exception as raised:
            error = raised
        saved = json.loads((self.root / 'state.json').read_text())
        evidence = json.loads((self.root / 'evidence.json').read_text())
        return key, error, saved, evidence, calls, persisted

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_authorized_incident_is_quarantined_once_and_one_fresh_fixture_published(self):
        key, error, saved, evidence, calls, persisted = self.flow(RunFlow().state_with('publish base'))
        self.assertRegex(str(error), 'stop at publication verification')
        record = saved['quarantine'][self.INCIDENT]
        self.assertEqual(record['status'], 'unresolved')
        self.assertEqual(record['reserved_mist'], 400_000_000)
        self.assertEqual(record['source']['run'], 40095854988)
        historical = record['publication']['pending_operation']  # Complete provenance kept, never cleared.
        self.assertTrue(historical['legacy'])
        self.assertEqual((historical['label'], historical['observed']['epoch']), ('publish base', 1262))
        self.assertEqual(len(calls), 1)  # Exactly one fresh publication.
        self.assertIn(self.INCIDENT, persisted[0]['quarantine'])  # Durable before the fresh one is signed.
        fresh = saved['publications'][key]
        self.assertEqual(fresh['base_package'], HEAD)
        self.assertNotIn('pending_operation', fresh)
        # The old reservation and this run's cost are accounted separately.
        self.assertEqual(evidence['funding']['held_mist'], 400_000_000)
        self.assertEqual(evidence['funding']['required_mist'], 1_200_000_000)
        self.assertEqual([t['label'] for t in evidence['transactions']], ['publish base'])
        self.assertEqual(evidence['allowance_used_mist'], 89_000_000)
        self.assertEqual(evidence['quarantine'][self.INCIDENT]['reserved_mist'], 400_000_000)
        self.assertNotIn('publication', evidence['quarantine'][self.INCIDENT])

    def test_quarantine_is_one_time_even_if_the_historical_marker_returns(self):
        def state(key, digest, tools):
            value = RunFlow().state_with('publish base')(key, digest, tools)
            value['quarantine'] = {self.INCIDENT: {'fixture_key': key, 'status': 'unresolved', 'reserved_mist': 400_000_000,
                                                   'publication': {'pending_operation': 'publish base'}}}
            return value
        key, error, saved, evidence, calls, _ = self.flow(state)
        self.assertIsInstance(error, rc.Unexpired)
        self.assertEqual(calls, [])
        self.assertTrue(saved['publications'][key]['pending_operation']['legacy'])

    def test_unlisted_fixture_wallet_or_retained_identity_is_never_quarantined(self):
        for incident, key_override in ((False, None), (True, 'bytecode-v1:other@1.81.1')):
            with self.subTest(incident=incident, key=key_override):
                key, error, saved, _, calls, _ = self.flow(RunFlow().state_with('publish base'),
                                                           incident=incident, key_override=key_override)
                self.assertIsInstance(error, rc.Unexpired)
                self.assertEqual(calls, [])
                self.assertNotIn('quarantine', saved)
                self.assertTrue(saved['publications'][key]['pending_operation']['legacy'])
        publication = {'pending_operation': {'label': 'publish base', 'legacy': True}}
        with self.incident('K', operator=OPERATOR):
            state = {'publications': {'K': publication}}
            self.assertIsNone(live.quarantine_incident(state, 'K', ADDRESS, publication, rc.Unexpired('x'), 1.0))
        with self.incident('K'):
            retained = {'pending_operation': pending()}
            for value, error in ((publication, rc.Uncertain('ambiguous')), (retained, rc.Unexpired('x')),
                                 (dict(publication, base_package=PACKAGE), rc.Unexpired('x'))):
                state = {'publications': {'K': value}}
                self.assertIsNone(live.quarantine_incident(state, 'K', ADDRESS, value, error, 1.0))
                self.assertEqual(state['publications'], {'K': value})

    def test_funding_holds_the_historical_reservation_until_resolved(self):
        with self.incident('K'):
            pending_state = {'publications': {'K': {'pending_operation': 'publish base'}}}
            self.assertEqual(live.held_reservations(pending_state, ADDRESS), 400_000_000)
            self.assertEqual(live.held_reservations(pending_state, OPERATOR), 0)  # Other wallet.
            self.assertEqual(live.held_reservations({'publications': {}}, ADDRESS), 0)
            for status, held in (('unresolved', 400_000_000), ('never_executed', 0), ('executed_late', 0)):
                state = {'publications': {}, 'quarantine': {self.INCIDENT: {'status': status}}}
                self.assertEqual(live.held_reservations(state, ADDRESS), held)
        key, error, _, evidence, calls, _ = self.flow(RunFlow().state_with('publish base'), balance='1100000000')
        self.assertIsInstance(error, live.FundingShortfall)
        self.assertEqual(calls, [])
        self.assertEqual(evidence['funding']['required_mist'], 1_200_000_000)

    def test_observation_records_the_eventual_outcome_without_adopting_it(self):
        coin = tc.normal('0x' + 'c0' * 32)
        late = tc.normal('0x' + 'cb' * 32)

        def state(observed_coins):
            marker = {'label': 'publish base', 'legacy': True,
                      'observed': {'epoch': 1262, 'at': 1.0, 'coins': observed_coins}}
            return {'fixtures': {'K': {'upgrade_cap': CAP}}, 'publications': {},
                    'quarantine': {self.INCIDENT: {'fixture_key': 'K', 'status': 'unresolved',
                                                   'publication': {'pending_operation': marker}}}}
        cap = {'objectId': late, 'version': '1', 'package': PACKAGE, 'cap_version': '1', 'previous_transaction': 'OLD'}
        executed = state({})
        live.observe_quarantine(executed, 'K', OPERATOR, FakeChain(epoch=1263, transactions={'OLD': facts(digest='OLD')},
                                                                     caps=[cap]), BUILT, 5.0)
        record = executed['quarantine'][self.INCIDENT]
        self.assertEqual((record['status'], record['digest'], record['gas_mist']), ('executed_late', 'OLD', 341_000_000))
        self.assertIn(PACKAGE, record['isolated_objects'])
        self.assertEqual(executed['fixtures']['K'], {'upgrade_cap': CAP})  # Never adopted.

        waiting = state({coin: '3'})
        live.observe_quarantine(waiting, 'K', OPERATOR, FakeChain(epoch=1263), BUILT, 5.0)
        self.assertEqual(waiting['quarantine'][self.INCIDENT]['status'], 'unresolved')
        self.assertIn('could still execute', waiting['quarantine'][self.INCIDENT]['last_observation']['reason'])

        expired = state({coin: '3'})
        live.observe_quarantine(expired, 'K', OPERATOR, FakeChain(epoch=1264, versions={coin: '4'}), BUILT, 5.0)
        self.assertEqual(expired['quarantine'][self.INCIDENT]['status'], 'never_executed')

        unrecorded = state({})
        unrecorded['fixtures'] = {}  # The fresh fixture's own cap could be mistaken for the old one.
        live.observe_quarantine(unrecorded, 'K', OPERATOR, FakeChain(epoch=1264), BUILT, 5.0)
        self.assertNotIn('last_observation', unrecorded['quarantine'][self.INCIDENT])


class ClassifiedStatus(unittest.TestCase):
    def evidence(self, root, *values):
        for index, value in enumerate(values):
            (Path(root) / f'{index}.json').write_text(json.dumps(value))

    def test_failure_class_from_trusted_outcome_and_bounded_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(testnet_ci.failure_class('skipped', root), 'infrastructure')
            self.assertEqual(testnet_ci.failure_class('cancelled', root), 'infrastructure')
            self.assertEqual(testnet_ci.failure_class('skipped', root, dependencies='failure'), 'source')
            self.assertEqual(testnet_ci.failure_class('failure', root), 'source')  # No evidence.
            self.evidence(root, {'success': True}, {'success': False, 'failure_class': 'funding'})
            self.assertEqual(testnet_ci.failure_class('failure', root), 'funding')
            self.evidence(root, {'success': False, 'failure_class': 'uncertain'},
                          {'success': False, 'failure_class': 'source'})
            self.assertEqual(testnet_ci.failure_class('failure', root), 'source')
            self.evidence(root, {'success': False, 'failure_class': 'not-a-class'}, {'success': True})
            self.assertEqual(testnet_ci.failure_class('failure', root), 'source')
        with tempfile.TemporaryDirectory() as root:
            outside = Path(root) / 'outside.txt'
            outside.write_text(json.dumps({'success': False, 'failure_class': 'funding'}))
            (Path(root) / 'evidence').mkdir()
            (Path(root) / 'evidence' / 'link.json').symlink_to(outside)
            self.assertEqual(testnet_ci.failure_class('failure', Path(root) / 'evidence'), 'source')

    def test_description_keeps_success_format_and_classifies_failures(self):
        base = 'b' * 40
        self.assertEqual(testnet_ci.description(base, 'success'), 'base:' + base)
        self.assertEqual(testnet_ci.description(base, 'pending'), 'base:' + base)
        self.assertEqual(testnet_ci.description(base, 'error'), 'base:' + base)
        self.assertEqual(testnet_ci.description(base, 'failure', 'uncertain'), f'base:{base} class:uncertain')
        with self.assertRaises(AssertionError):
            testnet_ci.description(base, 'failure', 'other')

    def test_workflow_reports_cancellation_as_error_and_passes_acceptance_outcome(self):
        workflow = (ROOT / '.github/workflows/testnet.yml').read_text()
        self.assertIn("job.status == 'cancelled' && 'error'", workflow)
        self.assertIn('ACCEPTANCE_OUTCOME: ${{ steps.acceptance.outcome }}', workflow)
        self.assertIn('id: acceptance', workflow)
        self.assertIn('DEPENDENCIES_OUTCOME: ${{ steps.dependencies.outcome }}', workflow)


if __name__ == '__main__':
    unittest.main()
