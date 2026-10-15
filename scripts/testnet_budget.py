"""Conservative, non-replenishing allowance for every testnet mutation.

Refunds never enlarge the allowance. Unknown execution outcomes consume the full
reservation. Device gas is charged as a grant, including already-cached gas.
"""
class Spent(RuntimeError):
    pass


class Budget:
    def __init__(self, limit, evidence):
        if limit <= 0:
            raise ValueError('spend limit must be positive')
        self.limit, self.used, self.evidence = limit, 0, evidence

    @property
    def remaining(self):
        return self.limit - self.used

    def grant(self, amount):
        if amount < 0 or amount > self.remaining:
            raise Spent('device funding exceeds remaining allowance')
        self.used += amount

    def execute(self, client, label, command, *, gas=200_000_000, funding=0):
        # Refuse a useless tiny budget before even invoking publish/upgrade.
        available = self.remaining - funding
        if available < 2_000_000:
            raise Spent(f'insufficient remaining allowance before {label}')
        reserved = min(gas, available)
        self.used += reserved + funding
        entry = dict(label=label, gas_budget_mist=reserved, funding_mist=funding, status='unknown')
        self.evidence.append(entry)
        try:
            result = client(*command, '--gas-budget', str(reserved))
            effects = result['effects']
            status = effects['status']['status']
            gas_used = effects['gasUsed']
            cost = max(0, int(gas_used['computationCost']) + int(gas_used['storageCost']) - int(gas_used['storageRebate']))
            if cost > reserved:
                raise RuntimeError('execution gas exceeds reserved budget')
            entry.update(digest=result['digest'], status=status, gas_mist=cost)
            self.used -= reserved - cost
            if status != 'success':
                raise RuntimeError(f'{label}: execution effects {effects["status"]}')
            return result
        finally:
            entry['allowance_used_mist'] = self.used
