#!/usr/bin/env python3
"""Read-only effects enrichment of existing SF-200 receipts; never signs/spends."""
import argparse
import hashlib
import json
from pathlib import Path
from live_common import Live
from live_security_delay import record_net_gas

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--wallet', required=True)
p.add_argument('--receipt', nargs='+', required=True)
args = p.parse_args()
live = Live(args.wallet, 'testnet', 1)
live.preflight(auto_fund=False)
for filename in args.receipt:
    path = Path(filename)
    receipt = json.loads(path.read_text())
    assert receipt['chain'] == live.chain and receipt['operator'] == live.operator
    receipt['pre_net_gas_enrichment_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    live.evidence = receipt
    record_net_gas(live)
    path.write_text(json.dumps(receipt, indent=2)+'\n')
    print('Verified signed net gas for',len(receipt['transactions']),'transactions:',filename)
