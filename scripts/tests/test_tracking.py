import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("tracking", ROOT / "scripts/bootstrap_tracking.py")
tracking = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracking)


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(tracking.BACKLOG.read_text())

    def test_seed_dependencies_are_known_acyclic_and_publishable(self):
        ordered = tracking.ordered_issues(self.config)
        self.assertEqual(len(ordered), len(self.config["issues"]))
        seen = set()
        for issue in ordered:
            self.assertTrue(set(issue["depends_on"]) <= seen)
            self.assertTrue(issue["paths"] and issue["acceptance"] and issue["validation"])
            seen.add(issue["key"])

    def test_rejects_missing_dependencies_cycles_and_duplicate_keys(self):
        for kind in ["unknown", "cycle", "duplicate"]:
            config = copy.deepcopy(self.config)
            if kind == "unknown":
                config["issues"][0]["depends_on"] = ["SF-999"]
            elif kind == "cycle":
                config["issues"][0]["depends_on"] = ["SF-001"]
            else:
                config["issues"].append(config["issues"][0])
            with self.assertRaises(ValueError):
                tracking.ordered_issues(config)

    def test_reuses_marker_after_title_edit_and_rejects_duplicates(self):
        issue = self.config["issues"][0]
        existing = [{"number":1,"title":"Human changed title","body":tracking.marker(issue["key"])}]
        self.assertEqual(tracking.discover_issues([issue], existing)[issue["key"]]["number"], 1)
        with self.assertRaises(ValueError):
            tracking.discover_issues([issue], existing + existing)

    def test_body_links_dependencies_and_requires_evidence(self):
        issue = self.config["issues"][2]
        body = tracking.issue_body(issue, {"SF-002":{"html_url":"https://github.com/example/repo/issues/2"}})
        self.assertIn("https://github.com/example/repo/issues/2", body)
        self.assertIn(issue["validation"], body)
        self.assertIn("Only Codex", body)
        self.assertIn(tracking.marker(issue["key"]), body)

    def test_default_cli_is_offline_and_does_not_create_publication_state(self):
        before = tracking.STATE.read_bytes() if tracking.STATE.exists() else None
        result = subprocess.run([sys.executable, str(ROOT / "scripts/bootstrap_tracking.py")], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OFFLINE PLAN", result.stdout)
        self.assertIn(f"{len(self.config['issues'])} prepared issues", result.stdout)
        after = tracking.STATE.read_bytes() if tracking.STATE.exists() else None
        self.assertEqual(before, after)

    def test_state_is_written_as_complete_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            tracking.save_state({"project":{"url":"test"},"issues":{}}, path)
            self.assertEqual(json.loads(path.read_text())["project"]["url"], "test")
            self.assertEqual([item.name for item in path.parent.iterdir()], ["state.json"])

    def test_project_initialization_preserves_populated_fields(self):
        class FakeGh:
            def graphql(self, query, variables):
                return {"node":{"fieldValues":{"nodes":[
                    {"name":"In progress","field":{"name":"Status"}},
                    {"name":"Claude","field":{"name":"Agent"}},
                    {"text":"existing model","field":{"name":"Model"}}]}}}
        self.assertEqual(tracking.current_item_fields(FakeGh(), "item"), {"Status","Agent","Model"})

    def test_unknown_project_option_is_rejected_before_mutation(self):
        class FakeGh:
            def graphql(self, *args):
                raise AssertionError("mutation should never be attempted")
        with self.assertRaises(ValueError):
            tracking.set_option(FakeGh(), {"id":"project"}, "item", {"id":"field","name":"Agent","options":[]}, "Claude")

    def test_views_preserve_existing_configuration_and_filter_new_model_views(self):
        class FakeGh:
            def __init__(self):
                self.created = []
                self.filters = []
            def graphql(self, query, variables):
                if "query(" in query:
                    return {"node":{"views":{"nodes":[{"id":"existing","name":"All work","number":1,"layout":"TABLE_LAYOUT","filter":"user-filter"}],"pageInfo":{"hasNextPage":False}}}}
                if "createProjectV2View" in query:
                    item = variables["input"]
                    self.created.append(item)
                    return {"createProjectV2View":{"projectV2View":{"id":item["name"],"name":item["name"],"number":len(self.created)+1,"layout":item["layout"],"filter":""}}}
                self.filters.append(variables["input"])
                return {"updateProjectV2View":{"projectV2View":{"id":variables["input"]["viewId"]}}}
        fake = FakeGh()
        fields = {name:{"id":name} for name in ["Title","Status","Priority","Area","Phase","Agent","Model","Review model","Work branch"]}
        views = tracking.ensure_views(fake, {"id":"project"}, fields)
        self.assertEqual(views["All work"]["filter"], "user-filter")
        self.assertEqual(len(fake.created), 5)
        self.assertIn({"viewId":"Claude work","filter":"agent:Claude"}, fake.filters)
        self.assertTrue(all("Agent" in view["configuration"]["visibleFieldIds"] for view in fake.created))

    def test_native_dependencies_add_missing_edges_and_preserve_existing_edges(self):
        class FakeGh:
            def __init__(self):
                self.added = []
            def api(self, endpoint, method=None, payload=None, paginate=False):
                if method == "POST":
                    self.added.append(payload["issue_id"])
                    return {}
                return [{"id":1},{"id":99}]
        fake = FakeGh()
        issues = [{"key":"SF-003","depends_on":["SF-001","SF-002"]}]
        known = {"SF-001":{"id":1},"SF-002":{"id":2},"SF-003":{"number":3}}
        self.assertEqual(tracking.ensure_dependencies(fake, "example/repo", issues, known), 2)
        self.assertEqual(fake.added, [2])


if __name__ == "__main__":
    unittest.main()
