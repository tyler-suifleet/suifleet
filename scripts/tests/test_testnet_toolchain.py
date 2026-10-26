"""SF-056: pinned build/network CLIs, read-only protocol preflight and
verify-before-sign package transactions. Offline; no network or signing."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sui_toolchain as tc
from testnet_budget import Budget
import live_package_upgrade as live

ROOT = Path(__file__).resolve().parents[2]
SENDER = '0x' + '0e' * 32
CAP = '0x' + 'ca' * 32
PACKAGE = '0x' + 'ab' * 32
MODULES = [base64.b64encode(bytes([0xa1, 0x1c, 0xeb, 0x0b, n])).decode() for n in range(3)]
DEPS = [tc.normal('0x1'), tc.normal('0x2')]
DIGEST = list(range(32))
BUILT = {'modules': MODULES, 'dependencies': DEPS, 'digest': DIGEST}
IN_CONTAINER = bool(os.environ.get('INSIDE_DEV_CONTAINER'))
HAVE_SDK = (ROOT / 'dapp/node_modules/@mysten/sui').exists()

# Builds real TransactionData bytes offline with the pinned frontend SDK.
BUILDER = r'''
const path = require('node:path');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const req = createRequire(path.resolve('dapp/package.json'));
(async () => {
  const spec = JSON.parse(process.argv[1]);
  const { Transaction } = await import(pathToFileURL(req.resolve('@mysten/sui/transactions')));
  const tx = new Transaction();
  const ref = (id) => ({ objectId: id, version: '1', digest: '11111111111111111111111111111111' });
  tx.setSender(spec.sender); tx.setGasOwner(spec.gasOwner || spec.sender); tx.setGasBudget(spec.budget);
  tx.setGasPrice(1000); tx.setGasPayment([ref('0x' + '9'.repeat(64))]); tx.setExpiration({ None: true });
  if (spec.upgrade) {
    const cap = tx.objectRef(ref(spec.upgrade.cap));
    const ticket = tx.moveCall({ target: '0x2::package::authorize_upgrade',
      arguments: [cap, tx.pure.u8(0), tx.pure.vector('u8', spec.digest)] });
    const receipt = tx.upgrade({ modules: spec.modules, dependencies: spec.dependencies,
      package: spec.upgrade.package, ticket });
    tx.moveCall({ target: '0x2::package::commit_upgrade', arguments: [spec.commitCap ? tx.objectRef(ref(spec.commitCap)) : cap, receipt] });
  } else {
    const cap = tx.publish({ modules: spec.modules, dependencies: spec.dependencies });
    tx.transferObjects([cap], spec.recipient || spec.sender);
  }
  if (spec.extra) tx.moveCall({ target: '0x2::coin::zero', typeArguments: ['0x2::sui::SUI'] });
  process.stdout.write(Buffer.from(await tx.build()).toString('base64'));
})().catch((e) => { console.error(e); process.exit(1); });
'''


def transaction(**spec):
    spec = {'sender': SENDER, 'budget': 400_000_000, 'modules': MODULES, 'dependencies': DEPS,
            'digest': DIGEST, **spec}
    built = subprocess.run(['node', '-e', BUILDER, json.dumps(spec)], cwd=ROOT, capture_output=True,
                           text=True, check=True, timeout=120)
    return tc.decode(built.stdout)


def tools(network='sui 1.82.1-987123842623', build='sui 1.81.1-e04cf9aa915c'):
    versions = {'/b/sui': build, '/n/sui': network}
    run = Mock(side_effect=lambda cmd, **_: SimpleNamespace(returncode=0, stdout=versions[cmd[0]] + '\n'))
    return run


class ToolIdentity(unittest.TestCase):
    def check(self, run, digest=tc.NETWORK['binary_sha256']):
        with unittest.mock.patch.object(tc, 'resolve', side_effect=lambda t: t):
            return tc.check_tools('/b/sui', '/n/sui', run=run, digest=lambda _: digest)

    def test_exact_pins_accepted_with_provenance(self):
        found = self.check(tools())
        self.assertEqual(found['build']['max_protocol'], 138)
        self.assertEqual(found['network']['max_protocol'], 139)
        self.assertEqual(found['network']['sha256'], tc.NETWORK['binary_sha256'])

    def test_path_substitution_is_refused(self):
        for run in (tools(build='sui 1.82.1-987123842623'), tools(network='sui 1.81.1-e04cf9aa915c'),
                    tools(network='sui 1.83.0-000000000000')):
            with self.assertRaises(tc.Incompatible):
                self.check(run)

    def test_unpinned_network_binary_is_refused(self):
        with self.assertRaisesRegex(tc.Incompatible, 'checksum'):
            self.check(tools(), digest='0' * 64)


class ProtocolPreflight(unittest.TestCase):
    TOOLS = {'build': dict(tc.BUILD), 'network': dict(tc.NETWORK)}

    def test_supported_protocol(self):
        result = tc.preflight(self.TOOLS, {'chain_identifier': '4c78adac', 'epoch': 1254, 'protocol_version': 139},
                              '4c78adac')
        self.assertEqual(result['protocol_version'], 139)
        self.assertTrue(result['build_cli_offline'])

    def test_newer_protocol_refused_before_writes(self):
        with self.assertRaisesRegex(tc.Incompatible, '140 exceeds'):
            tc.preflight(self.TOOLS, {'chain_identifier': '4c78adac', 'protocol_version': 140}, '4c78adac')

    def test_pinned_build_cli_alone_would_be_refused(self):
        # The SF-050 failure: protocol 139 against a 138 transaction CLI.
        with self.assertRaisesRegex(tc.Incompatible, '139 exceeds'):
            tc.preflight({'build': dict(tc.BUILD), 'network': dict(tc.BUILD)},
                         {'chain_identifier': '4c78adac', 'protocol_version': 139}, '4c78adac')

    def test_missing_protocol_or_wrong_chain(self):
        for facts in ({'chain_identifier': '4c78adac'}, {'chain_identifier': '4c78adac', 'protocol_version': '139'},
                      {'chain_identifier': '35834a8a', 'protocol_version': 139}):
            with self.assertRaises(tc.Incompatible):
                tc.preflight(self.TOOLS, facts, '4c78adac')


class Pins(unittest.TestCase):
    def test_dockerfile_installs_the_pinned_clis(self):
        text = (ROOT / 'Dockerfile').read_text()
        arg = lambda name: re.search(rf'^ARG {name}=(\S+)$', text, re.M).group(1)
        self.assertEqual(arg('SUI_VERSION'), tc.BUILD['release'])
        self.assertEqual(arg('SUI_NETWORK_VERSION'), tc.NETWORK['release'])
        self.assertEqual(arg('SUI_NETWORK_SHA256'), tc.NETWORK['archive_sha256'])
        self.assertEqual(arg('SUI_NETWORK_BINARY_SHA256'), tc.NETWORK['binary_sha256'])
        self.assertIn(tc.NETWORK_DEFAULT, text)

    def test_versions_match_releases(self):
        for pin in (tc.BUILD, tc.NETWORK):
            self.assertEqual(pin['release'], 'testnet-v' + pin['version'].split()[1].split('-')[0])

    def test_framework_pins_are_provenance(self):
        pins = tc.framework_pins(ROOT / 'contracts/Move.lock')
        self.assertEqual(set(pins), {'MoveStdlib', 'Sui', 'sui_edge'})
        self.assertIn('rev = "', pins['Sui'])

    def test_fixture_key_binds_pair_and_compiler(self):
        self.assertEqual(live.fixture_key('a', 'a'), 'a@1.81.1-e04cf9aa915c')
        self.assertEqual(live.fixture_key('a', 'b'), 'a..b@1.81.1-e04cf9aa915c')
        self.assertNotEqual(live.fixture_key('a', 'b'), live.fixture_key('a', 'c'))

    @unittest.skipUnless(IN_CONTAINER, 'installed toolchain exists only in the dev image')
    def test_installed_toolchain(self):
        found = tc.check_tools('sui', tc.NETWORK_DEFAULT)
        self.assertEqual(found['network']['sha256'], tc.NETWORK['binary_sha256'])


class OfflineCompilation(unittest.TestCase):
    def test_release_compilation_disables_rpc_tree_shaking_and_keeps_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / 'Move.lock').write_text('reviewed lock')
            built = dict(modules=['module'], dependencies=['0x1'], digest=[1] * 32)
            run = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(built), stderr=''))
            self.assertEqual(tc.compile_package('/pinned/sui', package, run=run)['modules'], ['module'])
            self.assertIn('--no-tree-shaking', run.call_args.args[0])
            self.assertEqual((package / 'Move.lock').read_text(), 'reviewed lock')
            def changed(*args, **kwargs):
                (package / 'Move.lock').write_text('changed')
                return SimpleNamespace(returncode=0, stdout=json.dumps(built), stderr='')
            with self.assertRaisesRegex(RuntimeError, 'changed Move.lock'):
                tc.compile_package('/pinned/sui', package, run=changed)


class NotSubmittedBudget(unittest.TestCase):
    def test_refusal_before_signing_releases_reservation_only(self):
        rows = []
        budget = Budget(500_000_000, rows)
        submit = Mock(side_effect=tc.NotSubmitted('bytecode differs'))
        with self.assertRaises(tc.NotSubmitted):
            budget.execute(Mock(), 'publish base', submit, gas=400_000_000)
        submit.assert_called_once_with(400_000_000)
        self.assertEqual((budget.used, rows[0]['status']), (0, 'not_submitted'))

    def test_callable_result_is_charged_like_a_command(self):
        budget = Budget(500_000_000, [])
        effects = {'digest': 'd', 'effects': {'status': {'status': 'success'}, 'gasUsed': {
            'computationCost': '1000', 'storageCost': '2000', 'storageRebate': '500'}}}
        budget.execute(Mock(), 'upgrade', Mock(return_value=effects), gas=400_000_000)
        self.assertEqual(budget.used, 2500)

    def test_unknown_outcome_after_submission_still_consumes(self):
        budget = Budget(500_000_000, [])
        with self.assertRaises(TimeoutError):
            budget.execute(Mock(), 'publish', Mock(side_effect=TimeoutError()), gas=400_000_000)
        self.assertEqual(budget.used, 400_000_000)


@unittest.skipUnless(HAVE_SDK or IN_CONTAINER, 'pinned frontend SDK not installed (make dapp-install)')
class VerifyBeforeSign(unittest.TestCase):
    def test_publish_accepted(self):
        result = tc.verify_package_transaction(transaction(), BUILT, sender=SENDER, budget=400_000_000)
        self.assertEqual(result, {'kind': 'publish', 'modules': 3})

    def test_upgrade_accepted(self):
        data = transaction(upgrade={'cap': CAP, 'package': PACKAGE})
        result = tc.verify_package_transaction(data, BUILT, sender=SENDER, budget=400_000_000,
                                               upgrade={'cap': CAP, 'package': PACKAGE})
        self.assertEqual(result['kind'], 'upgrade')

    def test_tampering_refused(self):
        upgrade = {'cap': CAP, 'package': PACKAGE}
        other = '0x' + '77' * 32
        cases = [
            ({'modules': MODULES[:2] + [base64.b64encode(b'\xa1\x1c\xeb\x0b\x09').decode()]}, None, 'bytecode'),
            ({'modules': MODULES[:2]}, None, 'bytecode'),
            ({'dependencies': DEPS + [tc.normal('0x3')]}, None, 'dependencies'),
            ({'recipient': other}, None, 'UpgradeCap'),
            ({'gasOwner': other}, None, 'gas owner'),
            ({'budget': 400_000_001}, None, 'budget'),
            ({'extra': True}, None, 'commands'),
            ({'upgrade': upgrade, 'digest': list(range(1, 33))}, upgrade, 'digest'),
            ({'upgrade': {'cap': other, 'package': PACKAGE}}, upgrade, 'UpgradeCap'),
            ({'upgrade': {'cap': CAP, 'package': other}}, upgrade, 'package'),
            ({'upgrade': upgrade, 'commitCap': other}, upgrade, 'committed'),
            ({}, upgrade, 'commands'),
        ]
        for spec, expected, message in cases:
            with self.subTest(spec=spec, message=message):
                with self.assertRaisesRegex(tc.NotSubmitted, message):
                    tc.verify_package_transaction(transaction(**spec), BUILT, sender=SENDER, budget=400_000_000,
                                                  upgrade=expected)

    def test_exact_execution_signs_the_verified_bytes_only(self):
        # `sui client serialized-tx` re-derived a fresh ValidDuring nonce, so
        # the retained digest never executed (runs 40238821132/40243584986).
        with tempfile.TemporaryDirectory() as root:
            wallet = Path(root) / 'client.yaml'
            wallet.write_text(f"keystore:\n  File: {root}/sui.keystore\nenvs: []\nactive_address: '{SENDER}'\n")
            calls = []

            def signer(raw=None, address=SENDER, code=0):
                def run(command, **kwargs):
                    calls.append(command)
                    data = command[command.index('--data') + 1]
                    return SimpleNamespace(returncode=code, stderr='refused', stdout=json.dumps(
                        {'suiAddress': address, 'rawTxData': raw or data, 'suiSignature': 'SIG'}))
                return run
            self.assertEqual(tc.exact_execution('/n/sui', wallet, 'BYTES', run=signer()),
                             ['execute-signed-tx', '--tx-bytes', 'BYTES', '--signatures', 'SIG'])
            self.assertEqual(calls[0][:4], ['/n/sui', 'keytool', '--keystore-path', f'{root}/sui.keystore'])
            self.assertEqual(calls[0][calls[0].index('--address') + 1], tc.normal(SENDER))
            for run, message in ((signer(raw='OTHER'), 'exact verified bytes'),
                                 (signer(address='0x' + '0f' * 32), 'exact verified bytes'),
                                 (signer(code=1), 'could not be signed')):
                with self.assertRaisesRegex(tc.NotSubmitted, message):
                    tc.exact_execution('/n/sui', wallet, 'BYTES', run=run)
            wallet.write_text("keystore:\n  File: relative.keystore\nactive_address: '" + SENDER + "'\n")
            with self.assertRaisesRegex(tc.NotSubmitted, 'absolute'):
                tc.exact_execution('/n/sui', wallet, 'BYTES', run=signer())

    def test_submitter_signs_only_verified_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            package = Path(root)
            (package / 'Move.lock').write_text('pinned\n')
            tx_bytes = subprocess.run(['node', '-e', BUILDER, json.dumps({
                'sender': SENDER, 'budget': 400_000_000, 'modules': MODULES, 'dependencies': DEPS})],
                cwd=ROOT, capture_output=True, text=True, check=True).stdout
            build = SimpleNamespace(returncode=0, stdout=json.dumps(dict(BUILT, digest=DIGEST)), stderr='')
            calls = []

            def client(*command, json_output=True, check=True):
                calls.append(command[0])
                if command[0] == 'serialized-tx':
                    self.assertEqual(command[1], tx_bytes)
                    return {'effects': {}}
                return SimpleNamespace(returncode=0, stdout=tx_bytes + '\n', stderr='')

            def run(command, **kwargs):
                return build if command[1] == 'move' else subprocess.run(command, **kwargs)

            record = {}
            submit = tc.package_submitter(client, '/b/sui', package, [], sender=SENDER, run=run, record=record)
            submit(400_000_000)
            self.assertEqual(calls, ['test-publish', 'serialized-tx'])
            self.assertEqual(record['digest'], bytes(DIGEST).hex())
            calls.clear()
            tampered = dict(BUILT, modules=MODULES[:2])
            build.stdout = json.dumps(tampered)
            with self.assertRaises(tc.NotSubmitted):
                tc.package_submitter(client, '/b/sui', package, [], sender=SENDER, run=run)(400_000_000)
            self.assertNotIn('serialized-tx', calls)

            def relocking(*command, **kwargs):
                (package / 'Move.lock').write_text('re-pinned framework\n')
                return SimpleNamespace(returncode=0, stdout=tx_bytes, stderr='')
            build.stdout = json.dumps(BUILT)
            with self.assertRaisesRegex(tc.NotSubmitted, 'Move.lock'):
                tc.package_submitter(relocking, '/b/sui', package, [], sender=SENDER, run=run)(400_000_000)


class Pubfile(unittest.TestCase):
    def test_records_verified_publication(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'Pub.toml'
            tc.write_pubfile(path, '4c78adac', Path(root) / 'pkg', PACKAGE, CAP)
            text = path.read_text()
        self.assertIn(f'published-at = "{PACKAGE}"', text)
        self.assertIn(f'upgrade-capability = "{CAP}"', text)
        self.assertIn('toolchain-version = "1.81.1"', text)
        self.assertIn('chain-id = "4c78adac"', text)


if __name__ == '__main__':
    unittest.main()
