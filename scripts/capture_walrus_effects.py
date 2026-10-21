#!/usr/bin/env python3
"""Read-only independent verification of Testnet publisher responses.

Usage: capture_walrus_effects.py artifact-response.json recipe-response.json
Never uploads, signs, renews or deletes. Requires complete object history and
successful certification; preserves raw transaction/balance data for review.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
from capture_cost_quotes import read, TESTNET
from fleet_costs import effects_cost

OWNER = "0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965"


def capture(paths):
    facts = read(TESTNET, '{ chainIdentifier epoch { epochId } protocolConfigs { protocolVersion } }')
    number = 0
    for char in facts['chainIdentifier']:
        number = number * 58 + '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'.index(char)
    chain = number.to_bytes(32, 'big').hex()[:8]
    if chain != '4c78adac':
        raise RuntimeError('unexpected chain')
    result = {"network": "testnet", "chain": "4c78adac", "read_only": True,
              "observed_at_utc": datetime.now(timezone.utc).isoformat(), 'facts': facts,
              "uploads": [], "transactions": []}
    for kind, path in zip(("artifact", "recipe"), paths, strict=True):
        response = json.loads(Path(path).read_text())
        blob = response["newlyCreated"]["blobObject"]["id"]
        query = ('{ object(address:' + json.dumps(blob) + ') { address version storageRebate '
                 'owner { ... on AddressOwner { address { address } } } previousTransaction { digest } '
                 'asMoveObject { contents { json } } objectVersionsBefore(last:10) { nodes { version '
                 'previousTransaction { digest } } pageInfo { hasPreviousPage } } } }')
        obj = read(TESTNET, query)["object"]
        if obj["owner"]["address"]["address"] != OWNER:
            raise RuntimeError("blob not owned by dedicated agent")
        if obj["objectVersionsBefore"]["pageInfo"]["hasPreviousPage"]:
            raise RuntimeError("incomplete blob history")
        if obj["asMoveObject"]["contents"]["json"]["certified_epoch"] is None:
            raise RuntimeError("blob not certified")
        result["uploads"].append({"kind": kind, "publisher_response": response, "object": obj})
        digests = [n["previousTransaction"]["digest"] for n in obj["objectVersionsBefore"]["nodes"]]
        digests.append(obj["previousTransaction"]["digest"])
        for digest in digests:
            if any(t["digest"] == digest for t in result["transactions"]):
                continue
            query = ('{ transaction(digest:' + json.dumps(digest) + ') { digest transactionJson '
                     'sender { address } effects { status timestamp epoch { epochId } balanceChangesJson gasEffects { gasSummary { '
                     'computationCost storageCost storageRebate nonRefundableStorageFee } } } } }')
            tx = read(TESTNET, query)["transaction"]
            if tx["effects"]["status"] != "SUCCESS":
                raise RuntimeError("unsuccessful blob operation")
            result["transactions"].append(dict(kind=kind, **tx))
        blob_txs = [t for t in result["transactions"] if t["kind"] == kind]
        sui_delta = wal_delta = net_gas = 0
        for tx in blob_txs:
            gas = effects_cost(tx["effects"]["gasEffects"]["gasSummary"])
            net_gas += gas
            for change in tx["effects"]["balanceChangesJson"]:
                if change["coinType"].endswith("::sui::SUI"):
                    sui_delta += int(change["amount"])
                elif change["coinType"].endswith("::wal::WAL"):
                    wal_delta += int(change["amount"])
                else:
                    raise RuntimeError("unexpected token in publisher transaction")
        if sui_delta != -net_gas or wal_delta != -int(response["newlyCreated"]["cost"]):
            raise RuntimeError("publisher effects/token amounts do not reconcile")
        result["uploads"][-1]["accounting"] = {"sponsor_net_gas_mist": net_gas,
            "sponsor_wal_spent_frost": -wal_delta, "sui_reconciliation_residual": sui_delta + net_gas,
            "wal_reconciliation_residual": wal_delta + int(response["newlyCreated"]["cost"])}
    return result


if __name__ == "__main__":
    print(json.dumps(capture(sys.argv[1:]), indent=2))
