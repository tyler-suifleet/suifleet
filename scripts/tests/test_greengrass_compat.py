import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import greengrass_compat as compat
import greengrass_managed as managed

ROOT = Path(__file__).resolve().parents[2]


class RepositoryTests(unittest.TestCase):
    def test_committed_matrix_and_fixtures_pass(self):
        counts = compat.run(ROOT)
        self.assertEqual(set(counts), set(compat.SUITE_RUNNERS))
        self.assertTrue(all(count > 0 for count in counts.values()))

    def test_runtime_receipts_are_scoped_to_executed_suites(self):
        # SF-057 and SF-060 executed their declared suites; all others remain pending.
        matrix = json.loads((ROOT / compat.COMPAT / "greengrass-compat.json").read_text())
        self.assertEqual({name for name, s in matrix["suites"].items() if s["runtime_execution"] == "passed"},
                         {"credential-provider-cases.json", "cloudwatch-logs-cases.json"})


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
        versions = lambda c: next(x for x in c if x["id"] == "versions-are-integers-not-booleans")
        self.run_mutated("shadow-cases.json", lambda c: versions(c)["steps"][1].update(
            expect={"state": {"desired": {"enabled": True}, "delta": {"enabled": True}}, "version": 2}))
        self.run_mutated("shadow-cases.json", lambda c: versions(c)["steps"][5].update(expect={"version": 2}))
        self.run_mutated("stream-cases.json", lambda c: c[1]["steps"][5]["expect"].update(oldest=0))
        self.run_mutated("client-device-cases.json", lambda c: c[0]["requests"][9].update(allowed=True))

    def test_managed_expectations_pinned(self):
        tes = lambda c: next(x for x in c if x["id"] == "tes-defers-to-adapter")
        self.run_mutated("managed-dependency-cases.json", lambda c: tes(c)["expected"].update(outcome="replace"))
        self.run_mutated("managed-dependency-cases.json", lambda c: tes(c)["expected"].update(requires=["SF-022"]))
        self.run_mutated("managed-dependency-cases.json", lambda c: c[-1]["expected"].update(reason="invalid-dependency"))
        backoff = lambda c: next(x for x in c if x["id"] == "upstream-error-backoff")
        self.run_mutated("credential-provider-cases.json", lambda c: backoff(c)["steps"][3]["expect"].update(retry_after=120))
        revoke = lambda c: next(x for x in c if x["id"] == "stop-restart-and-revoke")
        self.run_mutated("credential-provider-cases.json", lambda c: revoke(c)["steps"][5].update(
            expect={"status": "ok", "credential_set": "set-1", "expiration": 3600, "upstream_calls": 1}))

    def test_cloudwatch_expectations_pinned(self):
        case = lambda c, cid: next(x for x in c if x["id"] == cid)
        retain = lambda c: case(c, "retention-replaces-oldest-in-same-namespace")["steps"]
        self.run_mutated("cloudwatch-metrics-cases.json", lambda c: retain(c)[11]["expect"].update(dropped=2))
        retry = lambda c: case(c, "retryable-and-invalid-results")["steps"]
        self.run_mutated("cloudwatch-metrics-cases.json", lambda c: retry(c)[4]["expect"].update(buffered=0))
        split = lambda c: case(c, "namespace-trigger-and-twenty-metric-calls")["steps"]
        self.run_mutated("cloudwatch-metrics-cases.json", lambda c: split(c)[6]["expect"]["calls"][0].update(count=21))
        lost = lambda c: case(c, "day-split-checkpoint-and-lost-response")["steps"]
        self.run_mutated("cloudwatch-logs-cases.json", lambda c: lost(c)[9]["expect"].update(spooled=0))
        self.run_mutated("cloudwatch-logs-cases.json", lambda c: lost(c)[5]["expect"]["uploads"][0].update(stream="/2025/10/09/thing/thing"))
        split = lambda c: case(c, "oversized-lines-split-at-utf8-boundaries")["steps"]
        self.run_mutated("cloudwatch-logs-cases.json", lambda c: split(c)[3]["expect"].update(events=1))
        self.run_mutated("cloudwatch-logs-cases.json", lambda c: split(c)[1]["expect"].update(events=2))
        alone = lambda c: case(c, "oversized-line-drops-only-itself")["steps"]
        self.run_mutated("cloudwatch-logs-cases.json", lambda c: alone(c)[2]["expect"].update(spooled=0))
        self.run_mutated("cloudwatch-logs-cases.json", lambda c: alone(c)[4]["expect"].update(uploads=[]))
        units = lambda c: case(c, "malformed-unit-types-change-nothing")["steps"]
        self.run_mutated("cloudwatch-metrics-cases.json", lambda c: units(c)[4]["expect"].update(status="success"))
        self.run_mutated("cloudwatch-metrics-cases.json", lambda c: units(c)[15]["expect"]["calls"][0].update(count=4))
        huge = lambda c: case(c, "huge-integer-values-change-nothing")["steps"]
        self.run_mutated("cloudwatch-metrics-cases.json", lambda c: huge(c)[2]["expect"].update(status="success"))
        self.run_mutated("cloudwatch-metrics-cases.json", lambda c: huge(c)[7]["expect"]["calls"][0].update(count=3))
        times = lambda c: case(c, "out-of-range-times-change-nothing")["steps"]
        self.run_mutated("cloudwatch-logs-cases.json", lambda c: times(c)[2]["expect"].update(status="spooled"))
        self.run_mutated("cloudwatch-logs-cases.json", lambda c: times(c)[7]["expect"].update(status="invalid"))
        names = lambda c: case(c, "levels-redaction-names-and-order")["steps"]
        self.run_mutated("cloudwatch-logs-cases.json", lambda c: names(c)[11]["expect"]["uploads"][1]["events"][1].__setitem__(1, "token=s3cr3t ok s3cr3t"))

    def test_managed_catalog_change_is_checked_against_fixtures(self):
        # Fixture outcomes depend on the catalog, so a catalog edit must be deliberate.
        def incomplete(c):
            next(a for a in c["adapters"] if a["id"] == "credential-provider").update(
                specification="incomplete", specification_gap="test")
        def roadmap(c):
            next(r for r in c["components"] if r["name"] == "variant.DLR").update(disposition="roadmap", tickets=["SF-064"])
        def ticket(c):
            next(a for a in c["adapters"] if a["id"] == "cloudwatch-metrics-export")["implementation_ticket"] = "SF-060"
        self.run_mutated("managed-components.json", incomplete)
        self.run_mutated("managed-components.json", roadmap)
        self.run_mutated("managed-components.json", ticket)


class ManagedCatalogGateTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads((ROOT / managed.CATALOG).read_text())
        self.matrix = json.loads((ROOT / compat.COMPAT / "greengrass-compat.json").read_text())
        backlog = json.loads((ROOT / "docs/tracking/backlog.json").read_text())
        self.keys = {issue["key"] for issue in backlog["issues"]}

    def errors(self, mutate, matrix=None):
        catalog = copy.deepcopy(self.catalog)
        mutate(catalog)
        return managed.check_catalog(ROOT, catalog, matrix or self.matrix, self.keys)

    def has(self, mutate, text, matrix=None):
        errors = self.errors(mutate, matrix)
        self.assertTrue(any(text in e for e in errors), errors)

    def component(self, catalog, name):
        return next(r for r in catalog["components"] if r["name"] == name)

    def adapter(self, catalog, aid):
        return next(a for a in catalog["adapters"] if a["id"] == aid)

    def test_baseline_has_no_errors(self):
        self.assertEqual(self.errors(lambda c: None), [])

    def test_adapters_link_published_tickets_and_scoped_execution(self):
        # Published scoped implementation tickets (Codex, PR #91 feedback).
        # Only the executed credential adapter has an owning runtime receipt.
        self.assertEqual({a["id"]: a["implementation_ticket"] for a in self.catalog["adapters"]}, {
            "credential-provider": "SF-057", "stream-export-aws": "SF-058",
            "cloudwatch-metrics-export": "SF-059", "cloudwatch-logs-export": "SF-060",
            "aws-iot-core-connection": "SF-061", "device-defender-metrics": "SF-062",
            "secure-tunneling": "SF-063"})
        self.assertEqual({a["id"] for a in self.catalog["adapters"] if a["runtime_execution"] == "passed"},
                         {"credential-provider", "cloudwatch-logs-export"})
        self.assertFalse(self.catalog["parity_claim"])
        for entry in self.catalog["components"] + self.catalog["platform_phases"]:
            self.assertNotIn("approval", entry)
        roadmap = [r["name"] for r in self.catalog["components"] if r["disposition"] == "roadmap"]
        self.assertTrue(roadmap)
        self.assertTrue(all("SF-064" in self.component(self.catalog, n)["tickets"] for n in roadmap))

    def test_required_families_and_parity(self):
        self.has(lambda c: c.update(parity_claim=True), "parity_claim")
        def drop(c):
            c["components"] = [r for r in c["components"] if r["family"] != "device-defender"]
            c["adapters"] = [a for a in c["adapters"] if a["id"] != "device-defender-metrics"]
        self.has(drop, "required family device-defender")

    def test_component_rules(self):
        self.has(lambda c: c["components"].append(copy.deepcopy(c["components"][0])), "duplicate")
        self.has(lambda c: self.component(c, "variant.DLR").update(tickets=[]), "needs owning tickets")
        self.has(lambda c: self.component(c, "variant.DLR")["tickets"].append("SF-999"), "unknown ticket SF-999")
        self.has(lambda c: self.component(c, "aws.greengrass.SageMakerEdgeManager").update(disposition="native-component", tickets=["SF-011"]),
                 "discontinued")
        self.has(lambda c: self.component(c, "aws.greengrass.SageMakerEdgeManager").update(disposition="roadmap", tickets=["SF-064"]),
                 "discontinued")
        self.has(lambda c: self.component(c, "aws.greengrass.SNS").update(tickets=["SF-020"]), "tracked by SF-064")
        self.has(lambda c: self.component(c, "aws.greengrass.SNS").update(disposition="exclude"), "disposition")
        self.has(lambda c: self.component(c, "aws.greengrass.Cloudwatch").pop("adapter"), "needs an adapter")
        self.has(lambda c: self.component(c, "aws.greengrass.Cloudwatch").update(adapter="nope"), "unknown adapter")
        self.has(lambda c: self.component(c, "aws.greengrass.Cloudwatch").update(adapter="credential-provider"), "does not list it")
        self.has(lambda c: self.component(c, "aws.greengrass.SNS")["aws"].update(os=["darwin"]), "aws type/os")
        self.has(lambda c: self.component(c, "aws.greengrass.SNS").update(disposition="maybe"), "disposition")
        self.has(lambda c: self.component(c, "aws.greengrass.SNS").update(native_equivalent=""), "native_equivalent")

    def test_catalog_records_no_approvals(self):
        # Roadmap gaps are not approved exclusions; the catalog cannot claim one.
        self.has(lambda c: self.component(c, "aws.greengrass.LambdaManager").update(approval="approved"), "approvals")
        self.has(lambda c: c["platform_phases"][4].update(approval="pending"), "approvals")

    def test_adapter_rules(self):
        self.has(lambda c: self.adapter(c, "credential-provider").update(ticket_request={"title": "x"}), "ticket_request is obsolete")
        self.has(lambda c: self.adapter(c, "credential-provider").update(implementation_ticket="SF-999"), "unknown implementation ticket")
        self.has(lambda c: self.adapter(c, "credential-provider").update(implementation_ticket=None), "unknown implementation ticket")
        self.has(lambda c: self.adapter(c, "credential-provider")["depends_on"].append("SF-057"), "its own implementation ticket")
        self.has(lambda c: self.adapter(c, "credential-provider").update(runtime_execution="passed", runtime_evidence=None), "runtime_evidence")
        self.has(lambda c: self.adapter(c, "credential-provider").update(runtime_execution="passed", runtime_evidence="docs/NOPE.json"),
                 "runtime_evidence")
        self.has(lambda c: self.adapter(c, "credential-provider").pop("runtime_execution"), "runtime_execution")
        self.has(lambda c: self.adapter(c, "credential-provider")["replaces"].update({"aws.greengrass.TokenExchangeService": "2.0.4"}), "!= reference")
        self.has(lambda c: self.adapter(c, "credential-provider")["replaces"].update({"aws.greengrass.SNS": "2.1.10"}), "does not reference")
        self.has(lambda c: self.adapter(c, "credential-provider")["platforms"].append("windows"), "no native runtime")
        self.has(lambda c: self.adapter(c, "credential-provider")["acceptance"].update(runtime_tests=[]), "runtime_tests")
        self.has(lambda c: self.adapter(c, "credential-provider")["acceptance"]["reference_suites"].append("x.json"), "unknown suite")
        self.has(lambda c: self.adapter(c, "credential-provider").update(licensing=""), "needs licensing")
        self.has(lambda c: self.adapter(c, "cloudwatch-logs-export")["replaces"].update({"aws.greengrass.LogManager": "unpinned"}), "pin an exact")
        self.has(lambda c: self.adapter(c, "cloudwatch-metrics-export")["acceptance"].update(reference_suites=[]), "executable reference suite")
        self.has(lambda c: self.adapter(c, "secure-tunneling").pop("specification_gap"), "specification_gap")
        self.has(lambda c: self.adapter(c, "secure-tunneling").update(specification="deferred"), "specification")
        self.has(lambda c: self.adapter(c, "aws-iot-core-connection").update(specification="complete"), "pin an exact")
        self.has(lambda c: c["adapters"].append(copy.deepcopy(c["adapters"][0])), "duplicate adapter")

    def test_document_lists_every_component_and_suite_size(self):
        doc = (ROOT / "docs/GREENGRASS_MANAGED_COMPONENTS.md").read_text()
        for row in self.catalog["components"]:
            self.assertIn(f"| `{row['name']}` |", doc)
        for adapter in self.catalog["adapters"]:
            self.assertIn(f"| `{adapter['id']}` | {adapter['implementation_ticket']} | {adapter['specification']} |", doc)
        counts = compat.run(ROOT)
        self.assertIn(f"| `managed-dependency-cases.json` | {counts['managed-dependency-cases.json']} classifications |", doc)
        for suite in ("credential-provider-cases.json", "cloudwatch-metrics-cases.json", "cloudwatch-logs-cases.json"):
            self.assertIn(f"| `{suite}` | {counts[suite]} steps", doc)
        compatibility = (ROOT / "docs/GREENGRASS_COMPATIBILITY.md").read_text()
        for suite, count in counts.items():
            self.assertIn(f"| `{suite}` | {count} ", compatibility)

    def test_every_platform_has_a_phase(self):
        phases = {p["platform"]: p["phase"] for p in self.catalog["platform_phases"]}
        self.assertEqual({p for p, v in phases.items() if v == "initial-prototype"}, {"linux-x86_64", "linux-aarch64", "linux-arm"})
        self.has(lambda c: c["platform_phases"].pop(4), "platform windows")
        self.has(lambda c: c["platform_phases"][0].update(phase="excluded"), "needs phase")
        self.has(lambda c: c["platform_phases"][4].update(tickets=[]), "tracked by SF-064")
        self.has(lambda c: c["platform_phases"][4].update(phase="initial-prototype"), "need a native runtime")
        self.has(lambda c: c["platform_phases"].append(copy.deepcopy(c["platform_phases"][0])), "duplicate")
        self.has(lambda c: c["platform_phases"][2].update(phase="roadmap", tickets=["SF-064"]), "not in the initial prototype phase")


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

    def test_shadow_version_must_be_integer_and_rejection_changes_nothing(self):
        store = compat.ShadowStore()
        created = store.apply({"op": "update", "request": {"state": {"desired": {"enabled": False}}}})
        self.assertEqual(created["version"], 1)
        for version in (True, False, "1", 1.5, None, [1], 0, -1):
            self.assertEqual(store.apply({"op": "update", "request": {"state": {"desired": {"enabled": True}}, "version": version}}),
                             {"error": "invalid"}, version)
            self.assertEqual(store.apply({"op": "delete", "version": version}), {"error": "invalid"}, version)
        self.assertEqual(store.apply({"op": "get"}), created)
        self.assertEqual(store.apply({"op": "delete", "version": 1}), {"version": 2})

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

    def test_version_ranges(self):
        self.assertTrue(managed.satisfies("2.0.3", "^2.0.0"))
        self.assertFalse(managed.satisfies("3.0.0", "^2.0.0"))
        self.assertTrue(managed.satisfies("0.2.5", "^0.2.0"))
        self.assertFalse(managed.satisfies("0.3.0", "^0.2.0"))
        self.assertFalse(managed.satisfies("0.0.4", "^0.0.3"))
        self.assertTrue(managed.satisfies("2.0.9", "~2.0.0"))
        self.assertFalse(managed.satisfies("2.1.0", "~2.0.0"))
        self.assertTrue(managed.satisfies("2.0.3", ">=2.0.0 <3.0.0"))
        self.assertFalse(managed.satisfies("2.0.0-rc.1", ">=2.0.0"))
        self.assertTrue(managed.satisfies("2.0.0", "=2.0.0"))
        self.assertTrue(managed.satisfies("2.0.0", "2.0.0"))
        # SemVer 2.0.0 item 11: numeric prerelease identifiers compare as
        # integers, below alphanumeric ones; a shorter prefix sorts first.
        chain = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
                 "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0-rc.2", "1.0.0-rc.10", "1.0.0"]
        for lower, higher in zip(chain, chain[1:]):
            self.assertTrue(managed.satisfies(higher, f">{lower}"), (higher, lower))
            self.assertFalse(managed.satisfies(lower, f">{higher}"), (lower, higher))
            self.assertTrue(managed.satisfies(lower, f"<{higher}"), (lower, higher))
        self.assertTrue(managed.satisfies("2.0.0-rc.10", ">2.0.0-rc.2"))
        self.assertFalse(managed.satisfies("2.0.0-rc.2", ">2.0.0-rc.10"))
        self.assertTrue(managed.satisfies("2.0.0-rc.10", "^2.0.0-rc.9"))
        self.assertTrue(managed.satisfies("2.0.0-1a", ">2.0.0-10"))
        self.assertFalse(managed.satisfies("3.0.0-0", "^2.0.0"))
        self.assertTrue(managed.satisfies("2.0.0-rc.01a", "=2.0.0-rc.01a"))
        for bad in ("2.x", "^1 || ^2", ">= 2.0.0", "", ">2.0.0-rc.01"):
            with self.assertRaises(ValueError):
                managed.satisfies("2.0.0", bad)

    def test_credential_provider_fixture_invariants(self):
        provider = managed.CredentialProvider()
        provider.apply({"op": "grant", "component": "c"})
        provider.apply({"op": "start", "component": "c", "token": "t"})
        with self.assertRaises(AssertionError):
            provider.apply({"op": "start", "component": "c", "token": "t"})
        with self.assertRaises(AssertionError):
            provider.apply({"op": "request", "method": "GET", "path": managed.CREDENTIAL_PATH, "authorization": "t", "now": 0})
        provider.apply({"op": "request", "method": "GET", "path": "/", "now": 10})
        with self.assertRaises(AssertionError):
            provider.apply({"op": "request", "method": "GET", "path": "/", "now": 9})

    def test_metric_request_validation_edges(self):
        def request(**data):
            return {"request": {"namespace": "N", "metricData": {"metricName": "m", "value": 1, **data}}}
        self.assertTrue(managed._metric_valid(request()))
        for value in (float("nan"), float("inf"), -float("inf")):
            self.assertFalse(managed._metric_valid(request(value=value)))
        self.assertTrue(managed._metric_valid(request(value=-1.174271e+108)))
        # JSON integers too large for a float are rejected by the range check without raising.
        huge = json.loads("1" + "0" * 400)
        for value in (huge, -huge, 2 * 10 ** 108):
            self.assertFalse(managed._metric_valid(request(value=value)))
        self.assertFalse(managed._metric_valid(request(timestamp=-huge)))
        self.assertTrue(managed._metric_valid(request(value=10 ** 100)))
        for timestamp in (float("nan"), float("inf"), -float("inf"), -1, True):
            self.assertFalse(managed._metric_valid(request(timestamp=timestamp)))
        for timestamp in (0, 0.0, 1700000000.5):
            self.assertTrue(managed._metric_valid(request(timestamp=timestamp)))
        self.assertFalse(managed._metric_valid(request(dimensions=[{"name": "a"}])))
        for unit in ([], {}, ["Count"], None, True, 5, "count"):
            self.assertFalse(managed._metric_valid(request(unit=unit)))
        self.assertTrue(managed._metric_valid(request(unit="Count")))
        self.assertFalse(managed._metric_valid({"request": {"namespace": "N" * 256, "metricData": {"metricName": "m", "value": 1}}}))
        exporter = managed.MetricsExport()
        exporter.apply({"op": "tick", "now": 5})
        with self.assertRaises(AssertionError):
            exporter.apply({"op": "tick", "now": 4})

    def test_log_batches_respect_put_log_events_limits(self):
        exporter = managed.LogsExport()
        self.assertEqual(exporter.apply({"op": "configure", "sources": {"app": "UserComponent"}}), {"status": "ok"})
        for index in range(managed.LOG_BATCH_EVENTS + 1):
            exporter.apply({"op": "log", "source": "app", "level": "INFO", "message": "m", "time": 1759968000 + index % 7})
        exporter.apply({"op": "upstream", "results": ["ok", "ok"]})
        uploads = exporter.apply({"op": "tick", "now": 300})["uploads"]
        self.assertEqual([len(u["events"]) for u in uploads], [managed.LOG_BATCH_EVENTS, 1])
        times = [event[0] for upload in uploads for event in upload["events"]]
        self.assertEqual(times, sorted(times))
        # Four maximum-size events fill a batch exactly; the fifth starts the next one.
        big = "x" * (managed.LOG_EVENT_BYTES - managed.LOG_EVENT_OVERHEAD)
        for index in range(5):
            exporter.apply({"op": "log", "source": "app", "level": "INFO", "message": big, "time": 1759968000 + index})
        exporter.apply({"op": "upstream", "results": ["ok", "ok"]})
        uploads = exporter.apply({"op": "tick", "now": 600})["uploads"]
        self.assertEqual([len(u["events"]) for u in uploads], [4, 1])
        with self.assertRaises(AssertionError):
            exporter.apply({"op": "tick", "now": 599})

    def test_oversized_log_lines_split_within_limits(self):
        def batch_bytes(upload):
            return sum(len(e[1].encode()) + managed.LOG_EVENT_OVERHEAD for e in upload["events"])

        for line in ("x" * managed.LOG_BATCH_BYTES, "\u20ac" * 350000, "a" + "\U0001f600" * 200000):
            exporter = managed.LogsExport()
            exporter.apply({"op": "configure", "sources": {"app": "UserComponent"}})
            logged = exporter.apply({"op": "log", "source": "app", "level": "INFO", "message": line, "time": 1759968000})
            self.assertEqual(logged["status"], "spooled")
            exporter.apply({"op": "upstream", "results": ["ok"] * logged["events"]})
            uploads = exporter.apply({"op": "tick", "now": 300})["uploads"]
            events = [e for u in uploads for e in u["events"]]
            self.assertEqual(len(events), logged["events"])
            self.assertEqual("".join(e[1] for e in events), line)
            self.assertTrue(all(len(e[1].encode()) + managed.LOG_EVENT_OVERHEAD <= managed.LOG_EVENT_BYTES for e in events))
            self.assertTrue(all(batch_bytes(u) <= managed.LOG_BATCH_BYTES for u in uploads))
        self.assertEqual(managed.LogsExport._split("\u20ac" * 87373), ["\u20ac" * 87372, "\u20ac"])
        self.assertEqual(managed.LogsExport._split(""), [""])

    def test_oversized_log_line_evicts_nothing(self):
        exporter = managed.LogsExport()
        exporter.apply({"op": "configure", "sources": {"app": "UserComponent"}, "spool_limit": 100})
        self.assertEqual(exporter.apply({"op": "log", "source": "app", "level": "INFO", "message": "keep", "time": 0}),
                         {"status": "spooled", "events": 1, "spooled": 1, "dropped": 0})
        self.assertEqual(exporter.apply({"op": "log", "source": "app", "level": "INFO", "message": "x" * 101, "time": 1}),
                         {"status": "dropped", "events": 1, "spooled": 1, "dropped": 1})
        self.assertEqual([e[3] for e in exporter.spool], ["keep"])

    def test_log_unhashable_sources_are_rejected(self):
        exporter = managed.LogsExport()
        for sources in ({"app": []}, {"app": {}}, {"app": None}):
            self.assertEqual(exporter.apply({"op": "configure", "sources": sources}), {"status": "invalid-config"})
        self.assertEqual(exporter.apply({"op": "configure", "sources": {"app": "UserComponent"}}), {"status": "ok"})
        for source in ([], {}):
            self.assertEqual(exporter.apply({"op": "log", "source": source, "level": "INFO", "message": "x", "time": 1}),
                             {"status": "unknown-source"})

    def test_log_numbers_must_be_finite(self):
        exporter = managed.LogsExport()
        for interval in (float("nan"), float("inf")):
            self.assertEqual(exporter.apply({"op": "configure", "sources": {"app": "UserComponent"}, "interval": interval}),
                             {"status": "invalid-config"})
        exporter.apply({"op": "configure", "sources": {"app": "UserComponent"}})
        for time in (float("nan"), float("inf"), -float("inf")):
            self.assertEqual(exporter.apply({"op": "log", "source": "app", "level": "INFO", "message": "m", "time": time}),
                             {"status": "invalid"})
        self.assertEqual(exporter.apply({"op": "configure", "interval": 10 ** 400}), {"status": "ok"})
        exporter.apply({"op": "configure", "interval": 300})
        # Times that cannot name a UTC day are refused before spooling, so a tick never fails formatting them.
        for time in (10 ** 400, -10 ** 400, 1e300, -1, managed.LOG_TIME_LIMIT):
            self.assertEqual(exporter.apply({"op": "log", "source": "app", "level": "INFO", "message": "m", "time": time}),
                             {"status": "invalid"})
        self.assertEqual(exporter.apply({"op": "log", "source": "app", "level": "INFO", "message": "m",
                                         "time": managed.LOG_TIME_LIMIT - 1})["status"], "spooled")
        exporter.apply({"op": "upstream", "results": ["ok"]})
        self.assertEqual(exporter.apply({"op": "tick", "now": 300})["uploads"][0]["stream"], "/9999/12/31/thing/thing")

    def test_selection_rule_grammar(self):
        with self.assertRaises(ValueError):
            compat.selection_matches("thingName: a*b", "ab")
        with self.assertRaises(ValueError):
            compat.selection_matches("attribute: x", "x")
        self.assertTrue(compat.selection_matches("thingName: *mid*", "a-mid-b"))
        self.assertTrue(compat.selection_matches("thingName: team\\:dev", "team:dev"))


if __name__ == "__main__":
    unittest.main()
