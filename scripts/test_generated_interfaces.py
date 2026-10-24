#!/usr/bin/env python3
"""Positive and negative ABI tests using real compiler summaries (no chain writes)."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import generate_interfaces as generator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary-dir", type=Path)
    args = parser.parse_args()
    if args.summary_dir:
        from check_contract_interfaces import read_summary
        modules, _ = read_summary(args.summary_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="abi-negative-") as directory:
            modules, _ = generator.summarize(generator.ROOT / "contracts", Path(directory))
    expected = json.loads(generator.CATALOG.read_text())["modules"]
    assert not generator.diff(expected, generator.normalize(modules), "compiled ABI")

    mutations = {}
    for label in ["event-type", "event-order", "key-layout", "call-type", "call-mutability", "return-type", "ability", "missing-event", "private-call", "extra-field"]:
        changed = deepcopy(modules)
        manager = changed["deployment_manager"]
        event = manager["structs"]["DeploymentCreated"]
        function = manager["functions"]["report_status"]
        if label == "event-type": event["fields"]["fields"]["version"]["type_"] = "u64"
        elif label == "event-order":
            a, b = event["fields"]["fields"]["component_name"], event["fields"]["fields"]["version"]
            a["index"], b["index"] = b["index"], a["index"]
        elif label == "key-layout": manager["structs"]["ReceiptsKey"]["fields"]["fields"]["dummy_field"]["type_"] = "u8"
        elif label == "call-type": function["parameters"][2]["type_"] = "u64"
        elif label == "call-mutability": function["parameters"][0]["type_"]["Reference"][0] = False
        elif label == "return-type": manager["functions"]["status_applied"]["return_"] = ["u64"]
        elif label == "ability": event["abilities"].append("Store")
        elif label == "missing-event": del manager["structs"]["DeploymentCreated"]
        elif label == "private-call": function["visibility"] = "Private"
        elif label == "extra-field": event["fields"]["fields"]["new_field"] = {"index": 8, "type_": "bool"}
        mutations[label] = changed
    for label, changed in mutations.items():
        assert generator.diff(expected, generator.normalize(changed), label), f"failed to reject {label}"
    # Ignored compiler documentation/index metadata is not wire drift.
    changed = deepcopy(modules)
    changed["deployment_manager"]["structs"]["DeploymentCreated"]["doc"] = "new documentation"
    assert generator.normalize(changed) == expected
    catalog = json.loads(generator.CATALOG.read_text())
    recipe = json.loads(generator.RECIPE.read_text())
    for unsupported in ["u128", "u16", "vector<bool>"]:
        changed = deepcopy(catalog)
        changed["modules"]["deployment_manager"]["functions"]["report_status"]["parameters"][2] = unsupported
        try:
            generator.generate(changed, recipe)
        except ValueError as error:
            assert "unsupported transaction parameter" in str(error)
        else:
            raise AssertionError(f"generator silently accepted {unsupported}")
    with tempfile.TemporaryDirectory(prefix="generated-drift-") as directory:
        with patch.object(generator, "ROOT", Path(directory)):
            outputs = generator.generate(catalog, recipe)
            for path, text in outputs.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            assert generator.check(catalog, recipe, modules) == []
            for path, text in outputs.items():
                path.write_text(text + "// stale generated file\n")
                assert any(str(path.relative_to(generator.ROOT)) in error for error in generator.check(catalog, recipe, modules))
                path.write_text(text)
    print("PASS: real compiled ABI plus 10 negative field/type/order/key/call/ability cases and every stale generated file")


if __name__ == "__main__":
    main()
