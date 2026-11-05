#!/usr/bin/env python3
"""Deploy reporting against the retained evaluation core with durable spending.

One private run directory is one non-replenishing 2 SUI ledger. Every unsigned
transaction digest and full reservation is fsynced before signing. An interrupted
or uncertain operation stops for explicit reconciliation; rerunning cannot reset
spending or blindly replay a successful/unknown operation.
"""
import argparse
import json
import os
import hashlib
import secrets
import subprocess
from pathlib import Path
from types import SimpleNamespace

import deployment_manifest as dm
import sui_toolchain as toolchain
from live_common import Live, ROOT, DAEMON, CLOCK, stage, key_wallet, wait_for, stop_daemon
from live_fleet_budget import record_publication

LIMIT = 2_000_000_000


def durable(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class EvaluationRun(Live):
    def __init__(self, args):
        super().__init__(args.wallet, 'testnet', LIMIT, args.compiler, args.network_cli)
        self.directory = args.runtime.resolve()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.journal = self.directory / 'run.json'
        if self.journal.exists():
            if not getattr(args, 'accept_release', False):
                raise RuntimeError('Existing run: reconcile its durable ledger; never reset/replay it')
            prior = json.loads(self.journal.read_text())
            assert prior.get('limit_mist') == LIMIT and prior.get('chain') == '4c78adac'
            assert prior.get('release_verified') and (not prior.get('acceptance_started') or (getattr(args, 'journal_native', False) and prior.get('completed') and not prior.get('native_journal_acceptance_started')))
            assert all(op['status'] == 'success' for op in prior['operations'].values())
            self.evidence = prior
            self.ledger.evidence = prior['transactions']
            self.ledger.grant(prior['allowance_used_mist'])
            return
        self.evidence.update(ticket='SF-251', model='gpt-6.1-sol', limit_mist=LIMIT,
                             operations={}, completed=False)
        self.save()

    def save(self):
        self.evidence['allowance_used_mist'] = self.ledger.used
        durable(self.journal, self.evidence)

    def pending(self, label, data):
        operation = self.evidence['operations'].setdefault(label, {})
        assert 'digest' not in operation, 'never replace a journaled transaction'
        operation.update(status='unsigned_verified', digest=data['digest'],
                         sender=data['sender'], gas_budget=data['gas_budget'])
        self.save()

    def exact_ptb(self, ptb, config=None, *, persist=None):
        return super().exact_ptb(ptb, config, persist=lambda data: self.pending(self.label, data))

    def execute(self, label, command, **options):
        assert label not in self.evidence['operations'], 'operation already journaled'
        self.label = label
        try:
            result = super().execute(label, command, **options)
            self.evidence['operations'][label].update(status='success', result=result)
            return result
        finally:
            self.save()

    def publish(self, label, directory, pubfile, upgrade=None, **options):
        return super().publish(label, directory, pubfile, upgrade,
                               persist=lambda data: self.pending(label, data))


def publish_dependency(live, label, directory):
    def submit(budget):
        built = toolchain.compile_package(live.build_sui, directory)
        lock = (directory/'Move.lock').read_bytes()
        serialized = live.client('publish', directory, '--silence-warnings', '--gas-budget', str(budget),
                                 '--serialize-unsigned-transaction', json_output=False, check=False)
        if (directory/'Move.lock').read_bytes() != lock:
            raise toolchain.NotSubmitted('pinned dependency lock changed')
        if serialized.returncode:
            raise toolchain.NotSubmitted('dependency serialization refused')
        tx_bytes = serialized.stdout.strip().splitlines()[-1]
        data = live.decode(tx_bytes)
        checked = toolchain.verify_package_transaction(data, built, sender=live.operator, budget=budget)
        live.evidence.setdefault('verified_package_transactions', {})[label] = checked
        live.pending(label, data)
        return toolchain.execute_exact(live.executor(), live.signer(None, live.operator), tx_bytes, data['digest'])
    return live.execute(label, submit, gas=300_000_000, require_full=True)


def accept(args):
    os.umask(0o077)
    live = EvaluationRun(args)
    daemon = None
    signer = None
    try:
        original_balance = live.evidence.get('start_balance_mist')
        live.assert_lock(); live.preflight(auto_fund=False)
        live.evidence.setdefault('preflights', []).append(dict(balance_mist=live.start_balance, phase='acceptance'))
        if original_balance is not None: live.evidence['start_balance_mist'] = original_balance
        manifest, release = live.evidence['manifest'], live.evidence['reporting_release']
        dm.verify(manifest)
        live.evidence['acceptance_started'] = True; live.save()
        pkg, registry = manifest['call_package_id'], manifest['shared_objects']['registry']['id']
        call = lambda module, fn: f'{pkg}::{module}::{fn}'
        customers = live.evidence.get('customers', [])
        if args.journal_native:
            live.evidence['native_journal_acceptance_started'] = True; live.save()
        for n in ((3, 4) if args.journal_native else (1, 2)):
            root = live.directory/f'customer-{n}'; root.mkdir(mode=0o700)
            wallet, address = key_wallet(live, secrets.token_hex(32), root, 'identity')
            funding = 180_000_000
            live.transaction(f'fund independent customer {n}', '--split-coins', 'gas', f'[{funding}]',
                '--assign','fund','--transfer-objects','[fund]',f'@{address}',budget=5_000_000,funding=funding)
            child = live.child(wallet, funding)
            result = live.transaction(f'create customer {n}', '--move-call',call('customer','create'),
                f'@{registry}',f'"SF251 independent customer {n}"',budget=30_000_000,sender=child)
            customer = live.created(result,'::customer::Customer')[0]
            fleet = dict(id=live.created(result,'::customer::Fleet')[0], customerId=customer,
                budgetId=live.created(result,'::fleet_budget::FleetBudget')[0],administrator=True,devices=[])
            request = root/'browser.json'
            def browser(action, budget=20_000_000):
                request.write_text(json.dumps(dict(manifest=manifest,release=release,fleet=fleet,
                    device=fleet['devices'][0] if fleet['devices'] else None,action=action,sender=address,gasBudget=budget)))
                return json.loads(subprocess.check_output(['node',str(ROOT/'scripts/live_evaluation_reporting.cjs'),str(request)],text=True,timeout=180))
            def app_transaction(action, calls):
                label = f'frontend {action} customer {n}'
                def submit(budget):
                    tx_bytes = browser(action,budget)['bytes']; data = live.decode(tx_bytes)
                    toolchain.verify_ptb_transaction(data,sender=address,budget=budget,calls=calls)
                    live.pending(label,data)
                    return toolchain.execute_exact(live.executor(wallet),live.signer(wallet,address),tx_bytes,data['digest'])
                return live.execute(label,submit,gas=20_000_000,sender=child,require_full=True)
            app_transaction('setup',[call('customer','borrow_budget_cap'),
                release['storagePackageId']+'::fleet_storage::new_quota',release['packageId']+'::fleet_data::provision',
                call('customer','return_budget_cap')])
            live.transaction(f'grant customer {n} test capacity','--move-call',call('customer','set_entitlement'),
                f'@{customer}',f'@{registry}','1u64','1000000u64','1u64',budget=10_000_000)
            result = live.transaction(f'register customer {n} device network account','--move-call',call('customer','register_device'),
                f'@{customer}',f'@{fleet["id"]}',f'@{fleet["budgetId"]}',f'@{registry}',f'"SF251 native producer {n}"',
                f'@{address}','"x86_64"',CLOCK,budget=50_000_000,sender=child)
            device = live.created(result,'::device_registry::Device')[0]
            cap = live.created(result,'::device_cap::DeviceCap')[0]
            fleet['devices'] = [device]
            app_transaction('enroll',[call('customer','borrow_budget_cap'),release['packageId']+'::fleet_data::enroll',call('customer','return_budget_cap')])
            initial = browser('read'); assert initial['health']['status'] == 'unreported'
            template = root/'template.toml'; state = root/'state'
            template.write_text(f'[device]\nkeypair_path={json.dumps(str(root/"identity/test.key"))}\ndevice_object_id="{device}"\ndevice_cap_id="{cap}"\n[apply]\ncommand="/bin/false"\n[runtime]\nstate_dir={json.dumps(str(state))}\npoll_seconds=3\nevent_discovery_seconds=1\n')
            output = root/'release'; dm.generate(manifest,output,template)
            with (output/'config.toml').open('a') as stream: stream.write(initial['settings'])
            live.assert_lock()
            journal = root/'native-transactions'; journal.mkdir(mode=0o700)
            socket = root/'sign.sock'
            config = root/'signer.json'
            config.write_text(json.dumps(dict(key=str(root/'identity/test.key'),socket=str(socket),
                journal=str(journal),device=device,fleet=initial['config']['fleetId'],package=release['packageId'])))
            with (root/'signer.log').open('w') as log:
                signer = subprocess.Popen(['node',str(ROOT/'scripts/evaluation_report_signer.cjs'),str(config)],stdout=log,stderr=log)
            wait_for(socket.exists,'acceptance signer not ready',signer,timeout=10)
            text=(output/'config.toml').read_text()
            text=text.replace('keypair_path='+json.dumps(str(root/'identity/test.key'))+'\n','')
            text += '\n[device.signer]\nkind="service"\nsocket='+json.dumps(str(socket))+'\n'
            (output/'config.toml').write_text(text)
            with (root/'daemon.log').open('w') as log:
                daemon = subprocess.Popen([str(DAEMON)],env={**os.environ,'EDGE_CONFIG':str(output/'config.toml')},stdout=log,stderr=log)
            receipt = state/'fleet-data/health.json'
            wait_for(receipt.exists,'actual daemon report not observed',daemon,timeout=180)
            stop_daemon(daemon); daemon = None
            native = json.loads(receipt.read_text()); effects = live.executed(native['digest'])
            assert effects['effects']['status']['status'] == 'success'
            verified = browser('read'); assert verified['health']['status'] in ('healthy','degraded')
            native_journal = None
            if signer:
                stop_daemon(signer); signer = None
                native_journal = json.loads((journal/(native['digest']+'.json')).read_text())
                assert native_journal['digest'] == native['digest']
                native_journal.pop('tx_bytes')
            customers.append(dict(address=address,fleet=fleet,config=verified['config'],
                before_health=initial['health'],health=verified['health'],native_digest=native['digest'],
                native_effects=effects['effects'],presign_native_journal=native_journal,settings_sha256=hashlib.sha256(initial['settings'].encode()).hexdigest()))
            live.evidence['customers'] = customers; live.save()
        assert customers[0]['address'] != customers[1]['address']
        assert customers[0]['config']['storageId'] != customers[1]['config']['storageId']
        live.evidence['completed'] = True
        live.evidence['end_balance_mist'] = live.balance()
    finally:
        if daemon: stop_daemon(daemon)
        if signer: stop_daemon(signer)
        live.save()


def run(args):
    os.umask(0o077)
    live = EvaluationRun(args)
    try:
        live.assert_lock()
        live.preflight(auto_fund=False)
        live.evidence['preflights'] = [dict(balance_mist=live.start_balance, phase='release')]
        assert live.operator == '0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965'
        before = json.loads((ROOT/'profiles/seal-testnet/deployment.json').read_text())
        dm.verify(before)
        assert live.owner(before['upgrade_cap_id']) == live.operator
        registry = before['shared_objects']['registry']['id']
        live.evidence['before'] = before
        live.save()
        tree = live.directory/'contracts'
        stage(ROOT/'contracts', tree)
        original = before['type_origins']['device_registry::DeviceRegistry']
        record_publication(tree, live.chain, original, before['upgrade_cap_id'],
                           before['call_package_id'], before['package_version'])
        # test-upgrade verifies compatibility before bytes are signed; the
        # submitter independently compares pinned-compiler modules/dependencies.
        toolchain.write_pubfile(live.directory/'upgrade.toml', live.chain, tree, before['call_package_id'],
                                before['upgrade_cap_id'], before['package_version'], original)
        result = live.publish('add reporting attachment to retained core', tree, live.directory/'upgrade.toml',
                              upgrade={'cap':before['upgrade_cap_id'], 'package':before['call_package_id']})
        package = live.published(result)
        record_publication(tree, live.chain, original, before['upgrade_cap_id'], package,
                           int(before['package_version'])+1)
        manifest = dm.resolve(SimpleNamespace(chain_identifier=live.chain, rpc=live.rpc_url,
            package=package, upgrade_cap=before['upgrade_cap_id'], registry=registry,
            network='testnet', publisher=before['walrus']['publisher_url'],
            aggregator=before['walrus']['aggregator_url']))
        for key, value in before['type_origins'].items():
            assert manifest['type_origins'][key] == value, 'prior origin changed'
        assert manifest['shared_objects']['registry'] == before['shared_objects']['registry']
        # Keep reviewed endpoints, treasury and subscription descriptors exactly.
        for key in ('walrus','treasury_payee','subscription_coins'):
            if key in before: manifest[key] = before[key]
        dm.verify(manifest)
        live.evidence['manifest'] = manifest
        live.save()
        packages = {}
        for name in ('fleet_storage','fleet_data'):
            directory = tree/name
            published = publish_dependency(live, 'publish evaluation '+name, directory)
            entry = dict(package=live.published(published), cap=live.created(published,'::package::UpgradeCap')[0])
            assert live.owner(entry['cap']) == live.operator
            facts = dm.chain_facts(live.rpc_url, entry['package'], entry['cap'])
            assert facts['chain_identifier'] == live.chain
            assert dm.upgrade_cap(facts['objects'][entry['cap']])[:2] == (entry['package'],'1')
            record_publication(directory, live.chain, entry['package'], entry['cap'])
            packages[name] = entry
            live.evidence['packages'] = packages
            live.save()
        live.evidence['reporting_release'] = dict(packageId=packages['fleet_data']['package'],
            upgradeCapId=packages['fleet_data']['cap'], storagePackageId=packages['fleet_storage']['package'],
            storageUpgradeCapId=packages['fleet_storage']['cap'])
        live.evidence['release_verified'] = True
    finally:
        live.save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wallet', required=True, type=Path)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--compiler', required=True)
    parser.add_argument('--network-cli', required=True)
    parser.add_argument('--accept-release', action='store_true')
    parser.add_argument('--journal-native', action='store_true')
    try:
        args = parser.parse_args()
        (accept if args.accept_release else run)(args)
    except Exception as error:
        print(f'Evaluation reporting stopped ({type(error).__name__}); inspect private durable run before retrying.')
        import traceback
        Path('/tmp/sf251-private-error.txt').write_text(traceback.format_exc())
        raise SystemExit(1)
