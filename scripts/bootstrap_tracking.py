#!/usr/bin/env python3
"""Preview the Codex-maintained backlog; --apply publishes it through authenticated gh.
AGENTS.md restricts ticket creation to Codex unless the user explicitly grants an exception.
This script cannot authenticate model identity and must not be used to bypass that rule.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BACKLOG = ROOT / "docs/tracking/backlog.json"
STATE = ROOT / "docs/tracking/github-state.json"
STATUS = [("Backlog", "GRAY"), ("Ready", "BLUE"), ("In progress", "YELLOW"),
          ("Review", "PURPLE"), ("Blocked", "RED"), ("Done", "GREEN")]


def ordered_issues(config):
    if config.get("schema_version") != 1:
        raise ValueError("unsupported backlog schema")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config.get("repository", "")):
        raise ValueError("invalid repository")
    issues = config["issues"]
    keys = [issue["key"] for issue in issues]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate tracking key")
    if any(not re.fullmatch(r"SF-[0-9]{3}", key) for key in keys):
        raise ValueError("invalid tracking key")
    for issue in issues:
        if issue["priority"] not in {"P0", "P1", "P2"} or not issue["acceptance"] or not issue["validation"]:
            raise ValueError(f"incomplete acceptance/verification: {issue['key']}")
        if set(issue["depends_on"]) - set(keys):
            raise ValueError(f"unknown dependency: {issue['key']}")
    ordered, pending = [], list(issues)
    while pending:
        known = {issue["key"] for issue in ordered}
        ready = [issue for issue in pending if set(issue["depends_on"]) <= known]
        if not ready:
            raise ValueError("cyclic issue dependencies")
        for issue in ready:
            ordered.append(issue)
            pending.remove(issue)
    return ordered


def marker(key):
    return f"<!-- suifleet-tracking:{key} -->"


def issue_title(issue):
    return f"[{issue['key']}] {issue['title']}"


def discover_issues(issues, existing):
    matched = {}
    for issue in issues:
        matches = [item for item in existing if marker(issue["key"]) in (item.get("body") or "")
                   or item.get("title") == issue_title(issue)]
        if len(matches) > 1:
            raise ValueError(f"multiple issues match {issue['key']}; resolve duplicates before publication")
        if matches:
            matched[issue["key"]] = matches[0]
    return matched


def issue_body(issue, known):
    dependencies = [known[key]["html_url"] if key in known else key for key in issue["depends_on"]]
    acceptance = "\n".join(f"- [ ] {text}" for text in issue["acceptance"])
    paths = ", ".join(f"`{path}`" for path in issue["paths"])
    return f"""{marker(issue['key'])}
## Description

{issue.get('description', issue['title'])}

## Problem and evidence

{issue['evidence']}

## Scope and ownership

Priority: {issue['priority']}; area: {issue['area']}; phase: {issue['phase']}.
Paths: {paths}
Implementation Agent/Model: Unassigned. Claim ownership in the Project and record the branch/model/test plan here.
Dependencies: {', '.join(dependencies) if dependencies else 'None'}.

## Acceptance criteria

{acceptance}

## Verification

{issue['validation']}
Record exact commands/results and explicitly distinguish local, live-network and hardware evidence.

## Ticket authority

Only Codex may create new tickets/issues unless the user explicitly authorizes another model.
Implementing this issue does not grant permission to create new issues. Report additional findings to Codex here or in the handoff.
"""


class Gh:
    def run(self, *args, payload=None, as_json=False):
        env = dict(os.environ, GH_HOST="github.com", GH_PROMPT_DISABLED="1")
        result = subprocess.run(["gh", *args], input=json.dumps(payload) if payload is not None else None,
                                text=True, capture_output=True, env=env)
        if result.returncode:
            raise RuntimeError(f"gh {' '.join(args[:3])} failed: {result.stderr.strip()}")
        return json.loads(result.stdout) if as_json else result.stdout.strip()

    def api(self, endpoint, *, method=None, payload=None, paginate=False):
        args = ["api", endpoint]
        if endpoint != "graphql":
            args += ["-H", "X-GitHub-Api-Version: 2026-03-10"]
        if method:
            args += ["--method", method]
        if payload is not None:
            args += ["--input", "-"]
        if paginate:
            args += ["--paginate", "--slurp"]
        response = self.run(*args, payload=payload, as_json=True)
        if paginate:
            return [item for page in response for item in page]
        return response

    def graphql(self, query, variables):
        response = self.api("graphql", payload={"query": query, "variables": variables})
        if response.get("errors"):
            raise RuntimeError(f"GitHub GraphQL error: {response['errors']}")
        return response["data"]


OPTIONS = {
    "Priority": ["P0", "P1", "P2"],
    "Agent": ["Unassigned", "Codex", "Claude", "Other"],
    "Phase": ["baseline", "ota", "fleet", "extended"],
}


def set_option(gh, project, item, field, value):
    options = {option["name"]: option["id"] for option in field.get("options", [])}
    if value not in options:
        raise ValueError(f"Project field {field['name']} lacks required option {value}")
    gh.graphql("""mutation($input: UpdateProjectV2ItemFieldValueInput!) {
        updateProjectV2ItemFieldValue(input: $input) { projectV2Item { id } }
    }""", {"input": {"projectId": project["id"], "itemId": item, "fieldId": field["id"],
                     "value": {"singleSelectOptionId": options[value]}}})


def current_item_fields(gh, item):
    data = gh.graphql("""query($id: ID!) { node(id: $id) { ... on ProjectV2Item {
        fieldValues(first: 100) { nodes {
            ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2SingleSelectField { name } } }
            ... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2Field { name } } }
        } }
    } } }""", {"id": item})
    node = data.get("node")
    if not node:
        raise RuntimeError("Project item disappeared before initialization")
    return {value["field"]["name"] for value in node["fieldValues"]["nodes"] if "field" in value}


def ensure_views(gh, project, fields):
    data = gh.graphql("""query($id: ID!) { node(id: $id) { ... on ProjectV2 {
        views(first: 100) { nodes { id name number layout filter } pageInfo { hasNextPage } }
    } } }""", {"id": project["id"]})
    connection = data["node"]["views"]
    if connection["pageInfo"]["hasNextPage"]:
        raise ValueError("View discovery incomplete; refusing duplicate view creation")
    existing = {view["name"]: view for view in connection["nodes"]}
    visible = [fields[name]["id"] for name in ["Title", "Status", "Priority", "Area", "Phase", "Agent", "Model", "Review model", "Work branch"]]
    desired = [("Workflow", "BOARD_LAYOUT", ""), ("All work", "TABLE_LAYOUT", ""),
               ("P0 release blockers", "TABLE_LAYOUT", "priority:P0"),
               ("Codex work", "TABLE_LAYOUT", "agent:Codex"),
               ("Claude work", "TABLE_LAYOUT", "agent:Claude"),
               ("Unassigned work", "TABLE_LAYOUT", "agent:Unassigned")]
    for name, layout, filter_text in desired:
        if name in existing:
            continue
        created = gh.graphql("""mutation($input: CreateProjectV2ViewInput!) {
            createProjectV2View(input: $input) { projectV2View { id name number layout filter } }
        }""", {"input": {"projectId": project["id"], "name": name, "layout": layout,
                         "configuration": {"visibleFieldIds": visible}}})["createProjectV2View"]["projectV2View"]
        if filter_text:
            gh.graphql("""mutation($input: UpdateProjectV2ViewInput!) {
                updateProjectV2View(input: $input) { projectV2View { id } }
            }""", {"input": {"viewId": created["id"], "filter": filter_text}})
            created["filter"] = filter_text
        existing[name] = created
    return {name: existing[name] for name, _, _ in desired}


def ensure_dependencies(gh, repo, issues, known):
    count = 0
    for issue in issues:
        if not issue["depends_on"]:
            continue
        endpoint = f"repos/{repo}/issues/{known[issue['key']]['number']}/dependencies/blocked_by"
        existing = {item["id"] for item in gh.api(endpoint + "?per_page=100", paginate=True)}
        for key in issue["depends_on"]:
            dependency_id = known[key]["id"]
            if dependency_id not in existing:
                gh.api(endpoint, method="POST", payload={"issue_id": dependency_id})
                existing.add(dependency_id)
            count += 1
    return count


def publish(config, gh):
    issues = ordered_issues(config)
    repo = config["repository"]
    gh.run("auth", "status", "--hostname", "github.com")
    metadata = gh.api(f"repos/{repo}")
    owner = metadata["owner"]["login"]
    existing = [item for item in gh.api(f"repos/{repo}/issues?state=all&per_page=100", paginate=True)
                if "pull_request" not in item]
    known = discover_issues(issues, existing)
    projects = gh.run("project", "list", "--owner", owner, "--limit", "1000", "--format", "json", as_json=True)["projects"]
    matches = [project for project in projects if project["title"] == config["project_title"]]
    if len(matches) > 1 or (not matches and len(projects) >= 1000):
        raise ValueError("Project discovery is ambiguous/incomplete; select the intended project manually")
    new_project = not matches
    project = matches[0] if matches else gh.run("project", "create", "--owner", owner,
        "--title", config["project_title"], "--format", "json", as_json=True)
    number = str(project["number"])
    if new_project:
        gh.run("project", "edit", number, "--owner", owner, "--visibility", "PRIVATE",
               "--readme", "SuiFleet OTA/fleet roadmap. Only Codex creates agent tickets unless the user explicitly authorizes another model. See AGENTS.md and docs/ISSUE_TRACKING.md.")
    gh.run("project", "link", number, "--owner", owner, "--repo", repo)
    fields = gh.run("project", "field-list", number, "--owner", owner, "--limit", "100", "--format", "json", as_json=True)["fields"]
    if new_project:
        status = next(field for field in fields if field["name"] == "Status")
        gh.graphql("""mutation($input: UpdateProjectV2FieldInput!) {
            updateProjectV2Field(input: $input) { projectV2Field { ... on ProjectV2SingleSelectField { id } } }
        }""", {"input": {"fieldId": status["id"], "singleSelectOptions": [
            {"name": name, "color": color, "description": f"Workflow state: {name}"} for name, color in STATUS]}})
    wanted = dict(OPTIONS, Area=sorted({issue["area"] for issue in issues}))
    for name in [*wanted, "Model", "Review model", "Work branch"]:
        if not any(field["name"] == name for field in fields):
            args = ["project", "field-create", number, "--owner", owner, "--name", name,
                    "--data-type", "SINGLE_SELECT" if name in wanted else "TEXT", "--format", "json"]
            if name in wanted:
                args += ["--single-select-options", ",".join(wanted[name])]
            fields.append(gh.run(*args, as_json=True))
    fields = {field["name"]: field for field in gh.run("project", "field-list", number,
        "--owner", owner, "--limit", "100", "--format", "json", as_json=True)["fields"]}
    for name, values in wanted.items():
        if not set(values) <= {option["name"] for option in fields[name].get("options", [])}:
            raise ValueError(f"Existing field {name} has incompatible options; preserve live assignments and reconcile manually")
    status_names = {option["name"] for option in fields["Status"].get("options", [])}
    initial = "Backlog" if "Backlog" in status_names else "Todo"
    if initial not in status_names or "Done" not in status_names:
        raise ValueError("Existing Status workflow lacks Backlog/Todo or Done; reconcile manually")
    if not metadata.get("has_issues"):
        gh.api(f"repos/{repo}", method="PATCH", payload={"has_issues": True})
    labels = {label["name"] for label in gh.api(f"repos/{repo}/labels?per_page=100", paginate=True)}
    desired_labels = {"codex:tracked": ("8250DF", "Codex-maintained seed issue; implementation ownership is in the Project")}
    for issue in issues:
        desired_labels[f"priority:{issue['priority']}"] = ({"P0":"B60205","P1":"FBCA04","P2":"0075CA"}[issue["priority"]], "Release/product priority")
        desired_labels[f"area:{issue['area']}"] = ("5319E7", "Implementation area")
        desired_labels[issue["kind"]] = ("0E8A16", "Work item category")
    for name, (color, description) in desired_labels.items():
        if name not in labels:
            gh.api(f"repos/{repo}/labels", method="POST", payload=dict(name=name, color=color, description=description))
    items = gh.run("project", "item-list", number, "--owner", owner, "--limit", "1000", "--format", "json", as_json=True)["items"]
    if len(items) >= 1000:
        raise ValueError("Project item discovery reached the limit; refusing incomplete duplicate detection")
    item_ids = {item["content"].get("url"): item["id"] for item in items if item.get("content")}
    for issue in issues:
        key = issue["key"]
        if key not in known:
            known[key] = gh.api(f"repos/{repo}/issues", method="POST", payload={
                "title": issue_title(issue), "body": issue_body(issue, known),
                "labels": ["codex:tracked", f"priority:{issue['priority']}", f"area:{issue['area']}", issue["kind"]]})
            print(f"Created {key}: {known[key]['html_url']}", flush=True)
        else:
            print(f"Reused {key}: {known[key]['html_url']}", flush=True)
        url = known[key]["html_url"]
        if url not in item_ids:
            item = gh.run("project", "item-add", number, "--owner", owner, "--url", url, "--format", "json", as_json=True)
            item_ids[url] = item["id"]
        item_id = item_ids[url]
        # Initialize only missing values; retries never reset an active model's status/assignment.
        present = current_item_fields(gh, item_id)
        values = {"Status": "Done" if known[key]["state"] == "closed" else initial,
                  "Priority": issue["priority"], "Area": issue["area"], "Phase": issue["phase"], "Agent": "Unassigned"}
        for name, value in values.items():
            if name not in present:
                set_option(gh, project, item_id, fields[name], value)
    dependency_count = ensure_dependencies(gh, repo, issues, known)
    views = ensure_views(gh, project, fields)
    state = {"repository": repo, "project": project, "views": views, "dependency_count": dependency_count, "issues": {
        key: {"number": item["number"], "url": item["html_url"], "id": item["id"], "node_id": item["node_id"],
              "project_item_id": item_ids[item["html_url"]]} for key, item in known.items()}}
    return state


def save_state(state, path=STATE):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".tracking-", delete=False) as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create/update remote tracking; Codex only unless explicitly excepted")
    parser.add_argument("--backlog", type=Path, default=BACKLOG)
    args = parser.parse_args()
    config = json.loads(args.backlog.read_text())
    issues = ordered_issues(config)
    if not args.apply:
        print(f"OFFLINE PLAN — {config['repository']} / {config['project_title']}")
        print(config["issue_creation_policy"])
        for issue in issues:
            print(f"{issue['key']} {issue['priority']} {issue['area']}: {issue['title']} (depends: {', '.join(issue['depends_on']) or 'none'})")
        print(f"{len(issues)} prepared issues. No GitHub reads, writes, authentication or local state changes performed.")
        return
    if not shutil.which("gh"):
        parser.error("Install/authenticate GitHub CLI in this environment; see docs/ISSUE_TRACKING.md")
    state = publish(config, Gh())
    save_state(state)
    print(f"Project: {state['project']['url']}")
    print(f"Recorded published IDs in {STATE.relative_to(ROOT)}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, KeyError) as error:
        raise SystemExit(f"Tracking setup stopped: {error}") from error
