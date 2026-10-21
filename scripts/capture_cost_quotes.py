#!/usr/bin/env python3
"""Read-only public Sui/Walrus facts and historical transaction effects.

No signing, wallet configuration, RPC writes or paid services. Save stdout as
evidence; errors are fatal rather than being recorded as zero-priced quotes.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.request import Request, urlopen

MAINNET = "https://graphql.mainnet.sui.io/graphql"
TESTNET = "https://graphql.testnet.sui.io/graphql"
WALRUS = "0x2134d52768ea07e8c43570ef975eb3e4c27a39fa6396bef985b5abc58d03ddd2"
RELAY = "https://upload-relay.mainnet.walrus.space/v1/tip-config"
TOKENS = "https://api.coingecko.com/api/v3/simple/price?ids=sui,walrus-2&vs_currencies=usd&include_last_updated_at=true"
ROOT = Path(__file__).resolve().parents[1]


def read(url, query=None):
    req = Request(url, data=json.dumps({"query": query}).encode() if query else None,
                  headers={"Content-Type": "application/json", "User-Agent": "SuiFleet-cost-audit/1"})
    with urlopen(req, timeout=30) as response:
        result = json.load(response, parse_float=str)
    if query:
        if result.get("errors"):
            raise RuntimeError(result["errors"])
        return result["data"]
    return result


def main():
    facts_query = "{ chainIdentifier epoch { epochId referenceGasPrice } protocolConfigs { protocolVersion } }"
    walrus_query = ('{ object(address:' + json.dumps(WALRUS) + ') { dynamicFields(first:10) { nodes { '
                    'name { json } value { __typename ... on MoveValue { json } '
                    '... on MoveObject { contents { json } } } } pageInfo { hasNextPage } } } }')
    raw = read(MAINNET, walrus_query)
    fields = raw["object"]["dynamicFields"]
    if fields["pageInfo"]["hasNextPage"]:
        raise RuntimeError("Walrus state pagination requires explicit handling")
    states = [n["value"].get("json", n["value"].get("contents", {}).get("json", {}))
              for n in fields["nodes"]]
    states = [s for s in states if "storage_price_per_unit_size" in s]
    if len(states) != 1:
        raise RuntimeError("ambiguous Walrus state")
    state = states[0]
    transactions = []
    # Re-read immutable historical effects; never rerun these transactions.
    for filename in ("sf022-seal-testnet-26f6529.json", "sf022-seal-testnet.json",
                     "sf022-package-upgrade-testnet.json"):
        evidence = json.loads((ROOT / "docs/validation" / filename).read_text())
        selected = evidence["transactions"] + [dict(t, label="device receipt")
                                                for t in evidence.get("report_transactions", [])]
        for tx in selected:
            digest = tx["digest"]
            if any(t["digest"] == digest for t in transactions):
                continue
            query = ('{ transaction(digest:' + json.dumps(digest) + ') { effects { status '
                     'gasEffects { gasSummary { computationCost storageCost storageRebate '
                     'nonRefundableStorageFee } } } } }')
            effects = read(TESTNET, query)["transaction"]
            if effects is None:
                raise RuntimeError(f"historical transaction unavailable: {digest}")
            transactions.append({"digest": digest, "label": tx["label"], "source": filename,
                                 "effects": effects["effects"]})
    print(json.dumps({"schema_version": 1, "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                      "read_only": True, "transactions_submitted": 0,
                      "sources": {"mainnet": MAINNET, "testnet": TESTNET, "walrus_object": WALRUS,
                                  "relay": RELAY, "token_market_quotes": TOKENS},
                      "queries": {"facts": facts_query, "walrus": walrus_query},
                      "mainnet": read(MAINNET, facts_query), "testnet": read(TESTNET, facts_query),
                      "walrus": {"epoch": state["committee"]["epoch"],
                                 "n_shards": state["committee"]["n_shards"],
                                 "storage_price_per_unit_size": state["storage_price_per_unit_size"],
                                 "write_price_per_unit_size": state["write_price_per_unit_size"]},
                      "relay": read(RELAY), "token_market_quotes": read(TOKENS),
                      "historical_testnet_transactions": transactions}, indent=2))


if __name__ == "__main__":
    main()
