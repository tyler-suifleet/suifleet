"""Replay public SF-221 effects without keys, network calls or testnet writes."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testnet_budget import Budget, Spent
from analyze_fixture_grouping import analyze


class FreshCostTests(unittest.TestCase):
    def assertion_receipt(self):
        path = Path(__file__).resolve().parents[2] / 'docs/validation/sf221-assertion-costs.json'
        receipt = json.loads(path.read_text())
        return receipt, {run['phase']: run for run in receipt['runs']}

    def test_actual_assertion_reduction_funds_device_but_refuses_deployment(self):
        receipt, runs = self.assertion_receipt()
        accounting = receipt['ordinary_accounting']
        self.assertFalse(receipt['acceptance_complete'])
        self.assertEqual(receipt['source']['saved_vs_previous_bytes'], 2706)
        for phase in ('pair-0-fresh', 'pair-0-retained'):
            run = runs[phase]
            ledger = Budget(run['max_spend_mist'], [])
            # These raw historical receipts predate explicit grant tracing.
            # The cached grant is declared as inferred, never hidden/released.
            if phase.endswith('retained'):
                ledger.grant(accounting['cached_inferred_preexisting_grant_mist'])
            for tx in run['transactions']:
                ledger.execute(None, tx['label'], lambda _, tx=tx: dict(
                    digest=tx['digest'], effects=dict(status=dict(status=tx['status']), gasUsed=tx['gas_used'])),
                    gas=tx['gas_budget_mist'], funding=tx['funding_mist'], require_full=True)
            self.assertEqual(ledger.used, run['allowance_used_mist'])
            if phase.endswith('fresh'):
                self.assertEqual(run['transactions'][-1]['funding_mist'], 30_000_000)
                client = Mock()
                with self.assertRaises(Spent):
                    ledger.execute(client, 'create 3 deployments via head', ['ptb'],
                                   gas=60_000_000, require_full=True)
                client.assert_not_called()
        self.assertEqual(accounting['remaining_deficit_mist'], 69_440_237)
        self.assertFalse(runs['pair-0-fresh']['success'])
        self.assertTrue(runs['pair-0-retained']['success'])

    def test_real_temporal_retirement_does_not_refill_or_skip_reservation(self):
        _, runs = self.assertion_receipt()
        temporal = runs['temporal']
        pending = runs['pair-0-retained']['rollout_audit_pending']
        self.assertTrue(temporal['success'])
        retained, = temporal['retained_audits']
        self.assertEqual(retained['record'], pending['record'])
        self.assertEqual(retained['checkpoint'], pending['checkpoint'])
        self.assertGreater(int(retained['proof']['lowest']), int(pending['checkpoint']))
        self.assertTrue(retained['proof']['audit']['complete'])
        tx, = temporal['transactions']
        self.assertEqual(tx['digest'], retained['cleanup_digest'])
        self.assertEqual(tx['gas_mist'], 0)
        self.assertGreater(int(tx['gas_used']['storageRebate']),
                           int(tx['gas_used']['computationCost']) + int(tx['gas_used']['storageCost']))
        ledger = Budget(1_600_000_000, [])
        ledger.grant(1_570_000_000)
        execute = lambda _: dict(digest=tx['digest'], effects=dict(
            status=dict(status=tx['status']), gasUsed=tx['gas_used']))
        ledger.execute(None, tx['label'], execute, gas=tx['gas_budget_mist'], require_full=True)
        self.assertEqual(ledger.used, 1_570_000_000)
        ledger.grant(20_000_000)
        with self.assertRaises(Spent):
            ledger.execute(None, tx['label'], execute, gas=tx['gas_budget_mist'], require_full=True)
        self.assertEqual(ledger.used, 1_590_000_000)

    def test_actual_factored_candidate_still_fails_fresh_ledger(self):
        path = Path(__file__).resolve().parents[2] / 'docs/validation/sf221-factoring-costs.json'
        receipt = json.loads(path.read_text())
        report = analyze(receipt)
        self.assertEqual(receipt['source']['saved_bytes'], 377)
        self.assertNotEqual(receipt['source']['original_digest'], receipt['source']['factored_digest'])
        self.assertEqual(report['measured_deficit_mist'], 90_225_837)
        self.assertFalse(receipt['acceptance_complete'])
        runs = {run['phase']: run for run in receipt['runs']}
        for phase in ('pair-0-fresh', 'pair-0-retained'):
            run = runs[phase]
            ledger = Budget(run['max_spend_mist'], [])
            for tx in run['transactions']:
                ledger.execute(None, tx['label'], lambda _, tx=tx: dict(
                    digest=tx['digest'], effects=dict(status=dict(status=tx['status']), gasUsed=tx['gas_used'])),
                    gas=tx['gas_budget_mist'], funding=tx['funding_mist'], require_full=True)
            self.assertEqual(ledger.used, run['allowance_used_mist'])
            if phase.endswith('fresh'):
                client = Mock()
                with self.assertRaises(Spent):
                    ledger.execute(client, 'top up device gas', ['ptb'], gas=10_000_000,
                                   funding=30_000_000, require_full=True)
                client.assert_not_called()
        # A passing, cheaper source candidate is still a different exact pair;
        # cached effects cannot certify its failed fresh allowance.
        self.assertFalse(runs['pair-0-fresh']['success'])
        self.assertTrue(runs['pair-0-retained']['success'])

    def test_global_cleanup_netting_still_needs_source_or_effects_reduction(self):
        path = Path(__file__).resolve().parents[2] / 'docs/validation/sf221-combined-batch-costs.json'
        report = analyze(json.loads(path.read_text()))
        self.assertEqual(report['measured_deficit_mist'], 93_121_037)
        self.assertEqual(report['discarded_negative_net_mist'], 49_975_603)
        self.assertEqual(report['hypothetical_global_net_deficit_mist'], 43_145_434)
        self.assertEqual(report['hypothetical_zero_computation_deficit_mist'], 19_275_434)
        self.assertEqual(report['funding_mist'], 30_000_000)
        self.assertFalse(report['acceptance_complete'])

    def test_grouping_experiment_rejects_mixed_provenance_and_unexplained_costs(self):
        path = Path(__file__).resolve().parents[2] / 'docs/validation/sf221-combined-batch-costs.json'
        for field in ('base', 'head', 'chain', 'operator'):
            receipt = json.loads(path.read_text())
            receipt['runs'][1][field] = 'unrelated'
            with self.assertRaisesRegex(ValueError, 'different ' + field):
                analyze(receipt)
        for mutation, error in (
            (lambda r: r['runs'][0]['transactions'][0].update(status='unknown'), 'known successful'),
            (lambda r: r['runs'][0]['transactions'][0].update(gas_mist=0), 'effects disagree'),
            (lambda r: r['runs'][1].update(allowance_used_mist=0), 'unexplained'),
        ):
            receipt = json.loads(path.read_text())
            mutation(receipt)
            with self.assertRaisesRegex(ValueError, error):
                analyze(receipt)

    def test_sf198_public_failure_uses_exact_distinct_compiler_fixture(self):
        evidence = Path(__file__).resolve().parents[2] / 'docs/validation'
        runs = json.loads((evidence / 'sf221-cost-investigation.json').read_text())['runs']
        run = next(r for r in runs if r['url'].endswith('/41392636950'))
        inventory = json.loads((evidence / 'sf221-bytecode-inventory.json').read_text())
        revisions = {r['ref']: r for r in inventory['revisions']}
        base, head = (revisions[run[k]] for k in ('base', 'head'))
        self.assertNotEqual(base['digest'], head['digest'])
        self.assertEqual(run['fixture'], 'bytecode-v1:' + base['digest'] +
                         '..bytecode-v1:' + head['digest'] + '@1.81.1-e04cf9aa915c')
        self.assertEqual(run['remaining_mist'], 24_090_518)
        self.assertEqual(run['allowance_used_mist'], 1_575_909_482)
        for revision in (base, head):
            self.assertEqual(revision['root_bytes'], sum(m['bytes'] for m in revision['modules']))
            # Metadata removal cannot explain a saving: table 0x10 is absent
            # in the pinned compiler's actual root modules for this pair.
            self.assertTrue(all('16' not in m['table_bytes'] for m in revision['modules']))

    def test_real_local_head_batch_cannot_fit_even_with_free_identity_and_net_only_reservation(self):
        receipt = Path(__file__).resolve().parents[2] / 'docs/validation/sf221-local-costs.json'
        local = json.loads(receipt.read_text())
        retained = next(r for r in local['runs'] if r['phase'] == 'pair-0-retained')
        batch = next(t for t in retained['transactions'] if t['label'] == 'create 3 deployments via head')
        self.assertEqual(batch['status'], 'success')
        self.assertEqual(batch['gas_mist'], 42_002_663)
        for run, ledger in self.ledgers():
            client = Mock()
            # This deliberately optimistic lower bound charges no identity gas
            # and no inbox. Even reserving only the measured net fee is too much.
            with self.assertRaises(Spent):
                ledger.execute(client, batch['label'], ['ptb'], gas=batch['gas_mist'], require_full=True)
            client.assert_not_called()
            self.assertEqual(ledger.used, run['allowance_used_mist'])

    def test_actual_cross_pair_reuse_saves_funding_without_erasing_the_grant(self):
        receipt = Path(__file__).resolve().parents[2] / 'docs/validation/sf221-local-costs.json'
        runs = {r['phase']: r for r in json.loads(receipt.read_text())['runs']}
        fresh = runs['pair-1-fresh']
        self.assertEqual(fresh['preexisting_device_grant_mist'], 24_993_732)
        self.assertFalse(any(t['label'] == 'top up device gas' for t in fresh['transactions']))
        funding = next(t for t in runs['pair-0-retained']['transactions'] if t['label'] == 'top up device gas')
        self.assertEqual(funding['funding_mist'] + funding['gas_mist'] - fresh['preexisting_device_grant_mist'], 6_994_367)
        for phase in ('pair-0-retained', 'pair-1-fresh', 'pair-1-retained'):
            run = runs[phase]
            ledger = Budget(1_600_000_000, [])
            ledger.grant(run['preexisting_device_grant_mist'])
            for tx in run['transactions']:
                ledger.execute(None, tx['label'], lambda _, tx=tx: dict(digest=tx['digest'], effects={
                    'status': {'status': tx['status']}, 'gasUsed': tx['gas_used']}),
                    gas=tx['gas_budget_mist'], funding=tx['funding_mist'], require_full=True)
            self.assertEqual(ledger.used, run['allowance_used_mist'])

    def test_combined_fresh_and_retained_effects_do_not_establish_fresh_acceptance(self):
        path = Path(__file__).resolve().parents[2] / 'docs/validation/sf221-combined-batch-costs.json'
        runs = {r['phase']: r for r in json.loads(path.read_text())['runs']}
        fresh, retained = runs['pair-0-fresh'], runs['pair-0-retained']
        self.assertFalse(fresh['success'])
        self.assertTrue(retained['success'])
        self.assertEqual((fresh['base'], fresh['head']), (retained['base'], retained['head']))
        # Both ledgers must replay their own actual reservations and effects.
        # Separate cached success must never certify the failed fresh ledger.
        for run in (fresh, retained):
            ledger = Budget(run['max_spend_mist'], [])
            for tx in run['transactions']:
                ledger.execute(None, tx['label'], lambda _, tx=tx: dict(
                    digest=tx['digest'], effects=dict(status=dict(status=tx['status']), gasUsed=tx['gas_used'])),
                    gas=tx['gas_budget_mist'], funding=tx['funding_mist'], require_full=True)
            self.assertEqual(ledger.used, run['allowance_used_mist'])
        total = fresh['allowance_used_mist'] + retained['allowance_used_mist']
        self.assertEqual(total - fresh['max_spend_mist'], 93_121_037)
        # A deliberately optimistic fee-only replay cannot repair this pair:
        # erase *all* computation and funding, but retain the existing grouping
        # and non-replenishing refund rule. This is not a bound on regrouping
        # cleanup with creation or on different compatible source code.
        storage = sum(max(0, int(t['gas_used']['storageCost']) - int(t['gas_used']['storageRebate']))
                      for r in (fresh, retained) for t in r['transactions'])
        self.assertEqual(storage, 1_640_251_037)
        self.assertGreater(storage, fresh['max_spend_mist'])

    def ledgers(self):
        receipt = Path(__file__).resolve().parents[2] / 'docs/validation/sf221-cost-investigation.json'
        for run in json.loads(receipt.read_text())['runs']:
            ledger = Budget(run['max_spend_mist'], [])
            for tx in run['transactions']:
                effects = dict(status=dict(status=tx['status']), gasUsed=dict(
                    computationCost=str(tx['gas_mist']), storageCost='0', storageRebate='0'))
                ledger.execute(None, tx['label'], lambda reserved, tx=tx, effects=effects:
                               dict(digest=tx['digest'], effects=effects), gas=tx['gas_budget_mist'])
            self.assertEqual(ledger.used, run['allowance_used_mist'])
            self.assertEqual(ledger.remaining, run['remaining_mist'])
            yield run, ledger

    def test_measured_fresh_failure_is_allowance_not_wallet_balance(self):
        for run, ledger in self.ledgers():
            client = Mock()
            with self.assertRaisesRegex(Spent, 'before top up device gas'):
                ledger.execute(client, 'top up device gas', ['ptb'], gas=10_000_000,
                               funding=30_000_000, require_full=True)
            client.assert_not_called()
            self.assertEqual(ledger.used, run['allowance_used_mist'])
            if run['end_balance_mist'] is not None:
                self.assertGreater(run['end_balance_mist'], ledger.remaining)
                self.assertEqual(run['start_balance_mist'] - run['end_balance_mist'], run['spent_mist'])

    def test_reusing_funded_identity_does_not_remove_grant_or_full_budget_gate(self):
        for run, ledger in self.ledgers():
            # Synthetic reused balances bracket the measured remaining allowance.
            # No live funded identity or successful deployment is asserted here.
            with self.assertRaises(Spent):
                ledger.grant(30_000_000)
            ledger.grant(10_000_000)
            client = Mock()
            with self.assertRaisesRegex(Spent, 'full gas budget'):
                ledger.execute(client, 'required deployment batch', ['ptb'],
                               gas=200_000_000, require_full=True)
            client.assert_not_called()
            self.assertEqual(ledger.used, run['allowance_used_mist'] + 10_000_000)

    def test_cached_identity_gas_and_unknown_funding_remain_charged(self):
        ledger = Budget(1_600_000_000, [])
        ledger.grant(20_000_000)  # Synthetic cached account, no publication fees.
        client = Mock(side_effect=RuntimeError('unknown submission outcome'))
        with self.assertRaisesRegex(RuntimeError, 'unknown'):
            ledger.execute(client, 'top up', ['ptb'], gas=10_000_000, funding=30_000_000)
        self.assertEqual(ledger.used, 60_000_000)
        ledger.faucet(lambda: None)
        self.assertEqual(ledger.used, 60_000_000)
        self.assertEqual(ledger.evidence[-1]['status'], 'unknown')

class CompleteCompactAcceptance(unittest.TestCase):
    def test_actual_fresh_and_cached_ledgers_fit_without_replenishment(self):
        path = Path(__file__).resolve().parents[2] / 'docs/validation/sf221-compact-costs.json'
        receipt = json.loads(path.read_text())
        self.assertEqual(receipt['remaining_deficit_mist'], 0)
        for candidate in ('branch', 'larger_candidate'):
            runs = {r['phase']: r for r in receipt['measurements'][candidate]['runs']}
            fresh = runs['pair-0-fresh']
            for phase in ('pair-0-fresh', 'pair-0-retained'):
                run = runs[phase]
                self.assertTrue(run['success'])
                self.assertEqual(run['fixture'], fresh['fixture'])
                ledger = Budget(1_600_000_000, [])
                ledger.grant(run['preexisting_device_grant_mist'])
                for tx in run['transactions']:
                    ledger.execute(None, tx['label'], lambda _, tx=tx: dict(digest=tx['digest'],
                        effects=dict(status=dict(status=tx['status']), gasUsed=tx['gas_used'])),
                        gas=tx['gas_budget_mist'], funding=tx['funding_mist'], require_full=True)
                self.assertEqual(ledger.used, run['allowance_used_mist'])
            self.assertEqual(fresh['rollout_audit_pending']['record'], runs['temporal']['rollout_audit']['record'])
            temporal = runs['temporal']
            self.assertGreater(int(temporal['rollout_audit']['lowest']), int(temporal['rollout_audit']['checkpoint']))
            self.assertEqual(temporal['transactions'][0]['gas_budget_mist'], 20_000_000)
            self.assertEqual(temporal['allowance_used_mist'], 0)
            self.assertEqual([runs[p]['allowance_used_mist'] for p in ('wallet-fresh', 'wallet-retained')],
                             [101_000_198, 101_000_198])
