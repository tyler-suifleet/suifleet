#!/usr/bin/env python3
"""Automated SF-247 acceptance on the dedicated Testnet wallet only.

Two run-owned managed fleets discover independent locators without a rebuild;
the real daemon consumes the frontend-generated TOML and signs a health report.
Package publication is cached by exact source content and reverified on chain.
All mutations use Live's verify-before-sign/effects/strict allowance executor.
No existing customer/app objects are modified. Keep the private runtime on a
failure for explicit reconciliation; never automatically republish uncertainty.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import sys
from types import SimpleNamespace
import deployment_manifest as dm
from live_common import Live, ROOT, DAEMON, CLOCK, WALRUS_TESTNET, stage, key_wallet, plain, wait_for, stop_daemon
from live_fleet_budget import publish_storage, record_publication, net_gas


def restore_run(live, resume, ceiling, private_base):
    """Adopt known successful setup; never reset allowance or replay uncertainty."""
    assert resume['chain'] == live.chain and resume['operator'] == live.operator
    assert resume['max_spend_mist'] == ceiling, 'resume cannot raise the run allowance'
    assert not resume['completed']
    root = Path(resume['runtime']).resolve(strict=True)
    assert root.is_relative_to(private_base.resolve()) and root.name.startswith('sf247-reporting-')
    for transaction in resume['transactions']:
        assert transaction['status'] in ('success','failure'), 'unknown outcome needs explicit reconciliation'
        if transaction['status'] == 'failure':
            assert transaction['label'] == 'register device in first managed fleet'
            effects = live.executed(transaction['digest'])
            assert effects['effects']['status'] == dict(status='failure',error='InsufficientGas')
            assert not live.created(effects,'::device_registry::Device')
    live.ledger.grant(resume['allowance_used_mist'])
    live.evidence['transactions'].extend(resume['transactions'])
    live.evidence.update({k:v for k,v in resume.items() if k not in ('transactions','finished','end_balance_mist','spent_mist')})
    live.start_balance = resume['start_balance_mist']
    if resume.get('faucet'): live.ledger.faucet_attempts = 1
    return root


def run(args):
    os.umask(0o077)
    if not os.environ.get('SUIFLEET_TESTNET_LOCK_TOKEN'):
        raise RuntimeError('network:suifleet-testnet mutation lease required')
    live = Live(args.wallet, 'testnet', args.max_spend_mist)
    live.assert_lock()
    live.preflight(auto_fund=not args.resume_evidence)
    resume = json.loads(args.resume_evidence.read_text()) if args.resume_evidence else None
    if resume:
        root = restore_run(live, resume, args.max_spend_mist, Path(tempfile.gettempdir()))
        live.evidence['resumed_from'] = str(args.resume_evidence)
    else:
        root = Path(tempfile.mkdtemp(prefix='sf247-reporting-'))
    live.evidence.update(runtime=str(root), completed=False)
    sources = [*sorted((ROOT/'contracts/sources').glob('*.move')),
               ROOT/'contracts/fleet_storage/sources/fleet_storage.move',
               ROOT/'contracts/fleet_data/sources/fleet_data.move',
               ROOT/'dapp/src/lib/fleet-reporting.ts', ROOT/'dapp/src/lib/fleet-data.ts',
               ROOT/'suifleet-edge/src/fleet_data.rs', ROOT/'scripts/live_fleet_reporting.py',
               ROOT/'scripts/live_fleet_reporting.cjs']
    live.evidence['source_files_sha256'] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    if args.prior_publication_evidence:
        prior = json.loads(args.prior_publication_evidence.read_text())
        assert prior['chain'] == live.chain and prior['operator'] == live.operator
        assert prior['failed_publication_effects']['no_package_published']
        assert prior['failed_publication_effects']['status'] == dict(status='failure', error='InsufficientGas')
        assert all(t['funding_mist'] == 0 for t in prior['transactions']), 'unfinished funded identities need explicit recovery'
        live.evidence['prior_publication_run'] = dict(evidence=str(args.prior_publication_evidence),
            spent_mist=prior['spent_mist'], allowance_used_mist=prior['allowance_used_mist'],
            max_spend_mist=prior['max_spend_mist'], failed_digest=prior['failed_publication_effects']['digest'])
    state = args.wallet.parent / 'sf247-packages.json'
    cache = json.loads(state.read_text()) if state.exists() else {}
    daemon = None
    try:
        tree = root / 'contracts'; stage(ROOT / 'contracts', tree)
        def package(directory, dependency=''):
            digest = hashlib.sha256(dependency.encode())
            for source in sorted([directory/'Move.toml', directory/'Move.lock', *(directory/'sources').glob('*.move')]):
                digest.update(source.name.encode()+b'\0'+source.read_bytes()+b'\0')
            key = digest.hexdigest()
            if key in cache:
                entry = cache[key]
                facts = dm.chain_facts(live.rpc_url, entry['package'], entry['cap'])
                assert facts['chain_identifier'] == live.chain
                assert dm.upgrade_cap(facts['objects'][dm.object_id(entry['cap'])])[:2] == (entry['package'], '1')
                assert live.owner(entry['cap']) == live.operator
                live.check('reverified cached reporting package '+entry['package'])
            else:
                result = (live.publish('publish reporting core fixture', directory, root/'core-publication.toml')
                          if directory == tree else publish_storage(live, directory, 'publish reporting fixture '+directory.name))
                entry = dict(package=live.published(result), cap=live.created(result,'::package::UpgradeCap')[0])
                if directory == tree: entry['registry'] = live.created(result,'::device_registry::DeviceRegistry')[0]
                cache[key] = entry; state.write_text(json.dumps(cache,indent=2)+'\n')
            record_publication(directory, live.chain, entry['package'], entry['cap'])
            return entry
        core = package(tree)
        storage = package(tree/'fleet_storage',core['package'])
        data = package(tree/'fleet_data',core['package']+storage['package'])
        pkg, registry = core['package'], core['registry']
        facts = dm.chain_facts(live.rpc_url,pkg,core['cap'],registry)
        assert live.owner(core['cap']) == live.operator
        assert plain(live.object(registry)['content'])['admin'] == live.operator
        manifest = dm.resolve(SimpleNamespace(chain_identifier=live.chain,rpc=live.rpc_url,package=pkg,upgrade_cap=core['cap'],registry=registry,network='testnet',publisher=WALRUS_TESTNET[0],aggregator=WALRUS_TESTNET[1]))
        release = dict(packageId=data['package'],upgradeCapId=data['cap'],storagePackageId=storage['package'],storageUpgradeCapId=storage['cap'])
        call = lambda module, fn: f'{pkg}::{module}::{fn}'
        # Fresh test identity makes customer::create's idempotent registry key
        # unique, including retries with cached packages. Funding is charged once
        # against the parent ceiling and bounds every child/daemon transaction.
        def step(label, *ptb, **options):
            known = [t for t in (resume or {}).get('transactions',[]) if t['label']==label and t['status']=='success']
            assert len(known) <= 1
            if known:
                result = live.executed(known[0]['digest'])
                assert result['effects']['status']['status']=='success'
                return result
            return live.transaction(label,*ptb,**options)
        if resume:
            wallet = root/'device/client.yaml'
            address = dm.object_id(live.client('active-address',json_output=False,config=wallet).stdout.strip())
        else:
            seed = secrets.token_hex(32)
            wallet, address = key_wallet(live,seed,root,'device')
        funding = 160_000_000
        step('fund isolated customer/device identity','--split-coins','gas',f'[{funding}]','--assign','fund','--transfer-objects','[fund]',f'@{address}',budget=5_000_000,funding=funding)
        available = live.balance(address)
        if resume and available < 120_000_000:
            topup = 120_000_000 - available
            live.transaction('restore existing acceptance identity gas', '--split-coins','gas',f'[{topup}]','--assign','fund',
                '--transfer-objects','[fund]',f'@{address}',budget=5_000_000,funding=topup)
            available = live.balance(address)
        child = live.child(wallet,available)
        result = step('create first independent fleet','--move-call',call('customer','create'),f'@{registry}','"SF247 isolated customer"',budget=30_000_000,sender=child)
        customer = live.created(result,'::customer::Customer')[0]
        fleet1 = live.created(result,'::customer::Fleet')[0]; budget1 = live.created(result,'::fleet_budget::FleetBudget')[0]
        result = step('create second independent fleet','--move-call',call('customer','create_fleet'),f'@{customer}',f'@{registry}','"Second independent fleet"',budget=20_000_000,sender=child)
        fleet2 = live.created(result,'::customer::Fleet')[0]; budget2 = live.created(result,'::fleet_budget::FleetBudget')[0]
        fleets = [dict(id=fleet1,customerId=customer,budgetId=budget1,administrator=True,devices=[]),dict(id=fleet2,customerId=customer,budgetId=budget2,administrator=True,devices=[])]
        for n,fleet in enumerate(fleets):
            result = step(f'atomic reporting setup fleet {n+1}',
                '--move-call',call('customer','borrow_budget_cap'),f'@{customer}',f'@{fleet["id"]}','--assign','loan',
                '--move-call',f'{storage["package"]}::fleet_storage::new_quota','86400000u64','1048576u64','2u64','1048576u64','1000000u64','--assign','quota',
                '--move-call',f'{data["package"]}::fleet_data::provision',f'@{fleet["budgetId"]}','loan.0','quota',f'"{live.chain}"',
                '--move-call',call('customer','return_budget_cap'),f'@{fleet["id"]}','loan.0','loan.1',budget=20_000_000,sender=child)
            fleet['dataId'] = live.created(result,'::fleet_data::FleetData')[0]
        step('grant only test customer device capacity','--move-call',call('customer','set_entitlement'),f'@{customer}',f'@{registry}','1u64','1000000u64','1u64',budget=10_000_000)
        result = step('register device in first managed fleet','--move-call',call('customer','register_device'),f'@{customer}',f'@{fleet1}',f'@{budget1}',f'@{registry}','"SF247 health producer"',f'@{address}','"x86_64"',CLOCK,budget=50_000_000,sender=child)
        device = live.created(result,'::device_registry::Device')[0]
        device_cap = live.created(result,'::device_cap::DeviceCap')[0]
        account = live.created(result,'::fleet_budget::GasAccount')[0]
        fleets[0]['devices'] = [device]
        step('enable managed reporting writer','--move-call',call('customer','borrow_budget_cap'),f'@{customer}',f'@{fleet1}','--assign','loan',
            '--move-call',f'{data["package"]}::fleet_data::enroll',f'@{fleets[0]["dataId"]}',f'@{budget1}','loan.0',f'@{account}',
            '--move-call',call('customer','return_budget_cap'),f'@{fleet1}','loan.0','loan.1',budget=15_000_000,sender=child)
        request = root/'browser.json'; request.write_text(json.dumps(dict(manifest=manifest,release=release,fleets=fleets,device=device)))
        def browser():
            return json.loads(subprocess.check_output(['node',str(ROOT/'scripts/live_fleet_reporting.cjs'),str(request)],text=True,timeout=180))
        initial = browser(); assert initial['health']['status'] == 'unreported'
        template = root/'template.toml'; state_dir=root/'state'
        template.write_text(f'[device]\nkeypair_path={json.dumps(str(root/"device/test.key"))}\ndevice_object_id="{device}"\ndevice_cap_id="{device_cap}"\n[apply]\ncommand="/bin/false"\n[runtime]\nstate_dir={json.dumps(str(state_dir))}\npoll_seconds=3\nevent_discovery_seconds=1\n')
        output = root/'release'; dm.generate(manifest,output,template)
        with (output/'config.toml').open('a') as stream: stream.write(initial['settings'])
        live.assert_lock()
        with (root/'daemon.log').open('w') as log:
            daemon = subprocess.Popen([str(DAEMON)],env={**os.environ,'EDGE_CONFIG':str(output/'config.toml')},stdout=log,stderr=log)
        receipt = state_dir/'fleet-data/health.json'
        wait_for(receipt.exists,'actual daemon did not publish health with generated settings',daemon,timeout=180)
        stop_daemon(daemon); daemon=None
        native=json.loads(receipt.read_text()); result=live.executed(native['digest'])
        assert result['effects']['status']['status']=='success'
        verified=browser(); assert verified['health']['status'] in ('healthy','degraded')
        live.evidence.update(manifest=manifest,reporting_release=release,fleets=fleets,device=device,
            browser_health=verified['health'],settings_sha256=hashlib.sha256(initial['settings'].encode()).hexdigest(),
            native_health=dict(digest=native['digest'],net_gas_mist=net_gas(result)),runtime=str(root),
            limits=['No physical/mainnet/provider/manual-wallet acceptance','Empty storage provisioned; no archive purchase/upload claimed'])
        live.evidence['completed'] = True
        live.check('two new fleets dynamically resolved without rebuild; unreported before daemon; generated settings yielded independently verified signed native health')
    finally:
        if daemon: stop_daemon(daemon)
        live.finish(args.evidence)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wallet',type=Path,required=True)
    parser.add_argument('--max-spend-mist',type=int,default=1_000_000_000)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--prior-publication-evidence',type=Path)
    parser.add_argument('--resume-evidence',type=Path)
    try:
        run(parser.parse_args())
    except Exception as error:
        # subprocess exceptions can include signing/coordination command
        # arguments. Keep private diagnostics local; never dump such tracebacks.
        print(f'SF-247 acceptance stopped ({type(error).__name__}); inspect private runtime and evidence before retrying.', file=sys.stderr)
        sys.exit(1)
