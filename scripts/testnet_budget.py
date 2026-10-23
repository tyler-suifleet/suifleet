"""Conservative, non-replenishing allowance for every testnet mutation.

Refunds never enlarge the allowance. Unknown execution outcomes consume the full
reservation. Device gas is charged as a grant, including already-cached gas.
Only a transaction refused before it was signed (NotSubmitted) releases its
reservation.
"""
from sui_toolchain import NotSubmitted


class Spent(RuntimeError):
    pass


class Budget:
    def __init__(self, limit, evidence):
        if limit <= 0:
            raise ValueError('spend limit must be positive')
        self.limit, self.used, self.evidence = limit, 0, evidence
        self.faucet_attempts = 0

    @property
    def remaining(self):
        return self.limit - self.used

    def grant(self, amount):
        if amount < 0 or amount > self.remaining:
            raise Spent('device funding exceeds remaining allowance')
        self.used += amount

    def faucet(self, request):
        if self.faucet_attempts:
            raise Spent('one faucet attempt per run is allowed')
        self.faucet_attempts += 1
        return request()  # A credit cannot change used/remaining, even on failure.

    def execute(self, client, label, command, *, gas=200_000_000, funding=0, expect='success'):
        """Reserve gas plus funding, execute, then settle to the effects' net
        cost. `expect='failure'` is for calls that must abort on chain."""
        if gas <= 0 or funding < 0 or expect not in ('success', 'failure'):
            raise ValueError('invalid gas/funding reservation')
        # Refuse a useless tiny budget before even invoking publish/upgrade.
        available = self.remaining - funding
        if available < 2_000_000:
            raise Spent(f'insufficient remaining allowance before {label}')
        reserved = min(gas, available)
        self.used += reserved + funding
        entry = dict(label=label, gas_budget_mist=reserved, funding_mist=funding, status='unknown')
        self.evidence.append(entry)
        try:
            try:
                # A callable builds and verifies its transaction before submitting it.
                result = command(reserved) if callable(command) else client(*command, '--gas-budget', str(reserved))
            except NotSubmitted:
                self.used -= reserved + funding  # Refused before signing: nothing can be charged.
                entry['status'] = 'not_submitted'
                raise
            effects = result['effects']
            status = effects['status']['status']
            gas_used = effects['gasUsed']
            cost = max(0, int(gas_used['computationCost']) + int(gas_used['storageCost']) - int(gas_used['storageRebate']))
            if cost > reserved:
                raise RuntimeError('execution gas exceeds reserved budget')
            entry.update(digest=result['digest'], status=status, gas_mist=cost)
            self.used -= reserved - cost
            if status != expect:
                raise RuntimeError(f'{label}: expected {expect}, execution effects {effects["status"]}')
            return result
        finally:
            entry['allowance_used_mist'] = self.used
