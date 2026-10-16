import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import greengrass_compat as compat

ROOT = Path(__file__).resolve().parents[2]


class RepositoryTests(unittest.TestCase):
    def test_committed_matrix_and_fixtures_pass(self):
        counts = compat.run(ROOT)
        self.assertEqual(set(counts), set(compat.SUITE_RUNNERS))
        self.assertTrue(all(count > 0 for count in counts.values()))

    def test_every_suite_is_still_pending(self):
        # Flipping a suite to passed needs a runtime receipt from the owning
        # implementation; this specification ticket records none.
        matrix = json.loads((ROOT / compat.COMPAT / "greengrass-compat.json").read_text())
        self.assertEqual({s["runtime_execution"] for s in matrix["suites"].values()}, {"pending"})


class MatrixGateTests(unittest.TestCase):
    def setUp(self):
        self.matrix = json.loads((ROOT / compat.COMPAT / "greengrass-compat.json").read_text())
        backlog = json.loads((ROOT / "docs/tracking/backlog.json").read_text())
        self.keys = {issue["key"] for issue in backlog["issues"]}

    def errors(self, mutate):
        matrix = copy.deepcopy(self.matrix)
        mutate(matrix)
        return compat.check_matrix(ROOT, matrix, self.keys)

    def row(self, matrix, rid):
        return next(row for row in matrix["capabilities"] if row["id"] == rid)

    def test_baseline_has_no_errors(self):
        self.assertEqual(self.errors(lambda m: None), [])

    def test_parity_claim_rejected(self):
        self.assertIn("parity_claim must be false", self.errors(lambda m: m.update(parity_claim=True)))

    def test_aws_claim_without_passed_runtime_suite_rejected(self):
        def mutate(m):
            row = self.row(m, "shadows.local")
            row["aws_api"] = "implemented"
            row["current_evidence"] = ["docs/GREENGRASS_COMPATIBILITY.md"]
        self.assertTrue(any("passed runtime suite" in e for e in self.errors(mutate)))

    def test_passed_suite_needs_existing_receipt(self):
        def mutate(m):
            m["suites"]["shadow-cases.json"]["runtime_execution"] = "passed"
        self.assertTrue(any("runtime_evidence" in e for e in self.errors(mutate)))
        def missing(m):
            m["suites"]["shadow-cases.json"].update(runtime_execution="passed", runtime_evidence="docs/validation/none.json")
        self.assertTrue(any("runtime_evidence" in e for e in self.errors(missing)))

    def test_pending_suite_needs_owner(self):
        self.assertTrue(any("pending_on" in e for e in self.errors(lambda m: m["suites"]["stream-cases.json"].pop("pending_on"))))

    def test_partial_needs_existing_evidence(self):
        def missing(m):
            self.row(m, "recipe.format")["current_evidence"] = []
        self.assertTrue(any("requires current_evidence" in e for e in self.errors(missing)))
        def wrong(m):
            self.row(m, "recipe.format")["current_evidence"] = ["docs/NOPE.md"]
        self.assertTrue(any("missing evidence" in e for e in self.errors(wrong)))

    def test_unknown_tickets_suites_and_statuses_rejected(self):
        self.assertTrue(any("unknown ticket SF-999" in e for e in self.errors(lambda m: self.row(m, "shadows.local")["tickets"].append("SF-999"))))
        self.assertTrue(any("unknown suite" in e for e in self.errors(lambda m: self.row(m, "shadows.local")["suites"].append("x.json"))))
        self.assertTrue(any("status" in e for e in self.errors(lambda m: self.row(m, "shadows.local").update(functional="compatible"))))
        self.assertTrue(any("platform" in e for e in self.errors(lambda m: m["platforms"][2]["tickets"].append("SF-999"))))

    def test_duplicate_and_unowned_rows_rejected(self):
        self.assertTrue(any("duplicate" in e for e in self.errors(lambda m: m["capabilities"].append(copy.deepcopy(m["capabilities"][0])))))
        self.assertTrue(any("owning ticket" in e for e in self.errors(lambda m: self.row(m, "cli.local").update(tickets=[]))))


class FixtureGateTests(unittest.TestCase):
    """A wrong expectation in any suite must fail the checker."""

    def run_mutated(self, name, mutate):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / compat.COMPAT, root / compat.COMPAT)
            (root / "docs/tracking").mkdir(parents=True)
            shutil.copy(ROOT / "docs/tracking/backlog.json", root / "docs/tracking/backlog.json")
            for row in json.loads((ROOT / compat.COMPAT / "greengrass-compat.json").read_text())["capabilities"]:
                for path in row.get("current_evidence", []):
                    (root / path).parent.mkdir(parents=True, exist_ok=True)
                    (root / path).touch()
            for platform in json.loads((ROOT / compat.COMPAT / "greengrass-compat.json").read_text())["platforms"]:
                for path in platform.get("current_evidence", []):
                    (root / path).parent.mkdir(parents=True, exist_ok=True)
                    (root / path).touch()
            compat.run(root)  # unmodified copy passes
            path = root / compat.COMPAT / name
            value = json.loads(path.read_text())
            mutate(value)
            path.write_text(json.dumps(value))
            with self.assertRaises(AssertionError):
                compat.run(root)

    def test_recipe_outcome_and_translation_pinned(self):
        self.run_mutated("aws-recipe-cases.json", lambda c: c[0]["expected"].update(outcome="reject"))
        self.run_mutated("aws-recipe-cases.json", lambda c: c[0]["expected"]["manifests"][0]["recipe"].update(componentVersion="2.0.0"))
        self.run_mutated("aws-recipe-cases.json", lambda c: next(x for x in c if x["expected"]["outcome"] == "defer")["expected"].update(requires=[]))
        self.run_mutated("aws-recipe-cases.json", lambda c: c[0]["expected"].pop("manifests"))

    def test_pubsub_expectations_pinned(self):
        self.run_mutated("pubsub-cases.json", lambda c: c["matches"][0].update(matches=not c["matches"][0]["matches"]))
        self.run_mutated("pubsub-cases.json", lambda c: c["authorization"][1].update(allowed=True))

    def test_sequence_expectations_pinned(self):
        self.run_mutated("shadow-cases.json", lambda c: c[0]["steps"][0]["expect"].update(version=2))
        bools = lambda c: next(x for x in c if x["id"] == "booleans-are-not-numbers")
        self.run_mutated("shadow-cases.json", lambda c: bools(c)["steps"][0]["expect"]["state"]["delta"].update(enabled=1))
        self.run_mutated("shadow-cases.json", lambda c: bools(c)["steps"][0]["expect"]["state"]["delta"].pop("values"))
        self.run_mutated("stream-cases.json", lambda c: c[1]["steps"][5]["expect"].update(oldest=0))
        self.run_mutated("client-device-cases.json", lambda c: c[0]["requests"][9].update(allowed=True))


class ModelTests(unittest.TestCase):
    def test_json_equality_distinguishes_booleans_from_numbers(self):
        self.assertFalse(compat.json_equal(True, 1))
        self.assertFalse(compat.json_equal([False], [0]))
        self.assertFalse(compat.json_equal({"a": {"b": True}}, {"a": {"b": 1}}))
        self.assertFalse(compat.json_equal([1], [1, 1]))
        self.assertTrue(compat.json_equal({"a": [False, {"b": 2}]}, {"a": [False, {"b": 2}]}))

    def test_shadow_depth_counts_arrays_and_rejection_changes_nothing(self):
        deep = {"k12": 1}
        for i in range(11, 0, -1):
            deep = {f"k{i}": deep}
        store = compat.ShadowStore()
        self.assertEqual(store.apply({"op": "update", "request": {"state": {"desired": {"a": 1}}}})["version"], 1)
        for value in ([deep], [[[[[[[[1]]]]]]]], {"x": [[{"y": [[[[1]]]]}]]}):
            self.assertEqual(store.apply({"op": "update", "request": {"state": {"desired": {"values": value}}}}), {"error": "invalid"})
        self.assertEqual(store.apply({"op": "get"}), {"state": {"desired": {"a": 1}, "delta": {"a": 1}}, "version": 1})

    def test_translation_rejects_non_objects_and_reports_all_reasons(self):
        self.assertEqual(compat.translate_recipe([], {})["outcome"], "reject")
        result = compat.translate_recipe({"RecipeFormatVersion": "x", "ComponentType": "aws.greengrass.nucleus"}, {})
        self.assertEqual(result["outcome"], "reject")
        self.assertIn("unsupported-format-version", result["reasons"])
        self.assertIn("unsupported-component-type", result["reasons"])
        self.assertIn("no-manifests", result["reasons"])

    def test_invalid_topics_raise(self):
        with self.assertRaises(ValueError):
            compat.filter_matches("a/#/b", "a")
        with self.assertRaises(ValueError):
            compat.filter_matches("a", "a/+")

    def test_selection_rule_grammar(self):
        with self.assertRaises(ValueError):
            compat.selection_matches("thingName: a*b", "ab")
        with self.assertRaises(ValueError):
            compat.selection_matches("attribute: x", "x")
        self.assertTrue(compat.selection_matches("thingName: *mid*", "a-mid-b"))
        self.assertTrue(compat.selection_matches("thingName: team\\:dev", "team:dev"))


if __name__ == "__main__":
    unittest.main()
