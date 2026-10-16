"""SF-038 AWS-managed component catalog checks and adapter reference models.

Imported by scripts/greengrass_compat.py. Validates
interfaces/compat/managed-components.json and defines two *specification
reference models*: AWS recipe dependency classification against the catalog,
and the proposed credential-provider (token exchange equivalent) contract.
Like the SF-028 models, these make fixtures executable and self-consistent;
they are not an adapter and prove no runtime conformance.
"""
from __future__ import annotations

import re
from pathlib import Path

CATALOG = Path("interfaces/compat/managed-components.json")
AWS_TYPES = {"generic", "plugin", "lambda", "nucleus"}
AWS_OS = {"linux", "windows"}
SELECTIONS = {"recommended", "deferred"}
MANAGED_PREFIXES = ("aws.greengrass.", "aws.iot.", "variant.")
ADAPTER_TEXT = ("summary", "auth", "licensing")

NUM = r"(0|[1-9][0-9]*)"
IDENT = r"(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
CORE = rf"{NUM}\.{NUM}\.{NUM}(-{IDENT}(\.{IDENT})*)?"
COMPARATOR = re.compile(rf"^(>=|<=|>|<|=|\^|~)?({CORE})$")
# Same grammar as the native recipe schema and SF-037 translation.
RANGE = re.compile(rf"^(\*|(>=|<=|>|<|=|\^|~)?{CORE}( (>=|<=|>|<|=|\^|~)?{CORE})*)$")


# ---------------------------------------------------------------------------
# Version ranges (native comparator grammar) used for adapter compatibility.
# ---------------------------------------------------------------------------
def _key(version: str):
    """SemVer 2.0.0 precedence key (spec item 11).

    A prerelease sorts before its release. Prerelease identifiers compare left
    to right: numeric ones as integers and below alphanumeric ones, which
    compare as ASCII; a shorter identifier list that is a prefix sorts first.
    """
    core, _, pre = version.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    if not pre:
        return (major, minor, patch, 1, ())
    ids = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split("."))
    return (major, minor, patch, 0, ids)


def satisfies(version: str, requirement: str) -> bool:
    """True when version satisfies a native-grammar range (space means AND)."""
    if not RANGE.match(requirement):
        raise ValueError(f"unsupported range {requirement!r}")
    if requirement == "*":
        return True
    value = _key(version)
    for comparator in requirement.split(" "):
        op, bound = COMPARATOR.match(comparator).group(1, 2)
        low = _key(bound)
        major, minor, patch = low[:3]
        if op == "^":
            high = (major + 1, 0, 0, 0, ()) if major else (0, minor + 1, 0, 0, ()) if minor else (0, 0, patch + 1, 0, ())
            ok = low <= value < high
        elif op == "~":
            ok = low <= value < (major, minor + 1, 0, 0, ())
        else:
            ok = {">=": value >= low, "<=": value <= low, ">": value > low, "<": value < low}.get(op, value == low)
        if not ok:
            return False
    return True


# ---------------------------------------------------------------------------
# AWS recipe ComponentDependencies -> catalog disposition (proposed guidance
# for SF-037). Until SF-037 re-specifies translation, aws.greengrass.*
# dependencies stay rejected; this model fixes what each one will mean.
# ---------------------------------------------------------------------------
def classify_dependency(catalog: dict, name, spec) -> dict:
    if not isinstance(name, str) or not isinstance(spec, dict) or set(spec) - {"VersionRequirement", "DependencyType"}:
        return {"outcome": "reject", "reason": "invalid-dependency"}
    if spec.get("DependencyType", "HARD") not in ("HARD", "SOFT"):
        return {"outcome": "reject", "reason": "invalid-dependency"}
    requirement = spec.get("VersionRequirement", "*")
    if not isinstance(requirement, str) or not RANGE.match(requirement):
        return {"outcome": "reject", "reason": "unsupported-version-range"}
    row = next((c for c in catalog["components"] if c["name"] == name), None)
    if row is None:
        if name.startswith(MANAGED_PREFIXES):
            return {"outcome": "reject", "reason": "unknown-managed-component"}
        return {"outcome": "not-managed"}
    if "discontinued" in row:
        return {"outcome": "reject", "reason": "discontinued-aws-service"}
    disposition = row["disposition"]
    if disposition == "exclude":
        return {"outcome": "reject", "reason": "excluded-component"}
    if disposition == "not-applicable":
        return {"outcome": "reject", "reason": "not-applicable"}
    if disposition == "native":
        return {"outcome": "replace", "tickets": row["tickets"]}
    if disposition == "native-component":
        return {"outcome": "package-natively", "tickets": row["tickets"]}
    adapter = next(a for a in catalog["adapters"] if a["id"] == row["adapter"])
    if adapter["selection"] != "recommended":
        return {"outcome": "reject", "reason": "adapter-not-selected", "adapter": adapter["id"]}
    version = adapter["replaces"][name]
    if not satisfies(version, requirement):
        return {"outcome": "reject", "reason": "unsupported-aws-version", "adapter": adapter["id"]}
    return {"outcome": "defer", "adapter": adapter["id"], "requires": sorted(adapter["depends_on"]),
            "implementation_ticket": adapter["implementation_ticket"]}


def check_dependency_cases(cases, catalog: dict) -> int:
    for case in cases:
        actual = classify_dependency(catalog, case["dependency"], case["spec"])
        if actual != case["expected"]:
            raise AssertionError(f"managed dependency case {case['id']}: {actual!r} != {case['expected']!r}")
    return len(cases)


# ---------------------------------------------------------------------------
# Proposed credential-provider contract: the AWS container-credentials
# protocol consumed by unmodified AWS SDKs through
# AWS_CONTAINER_CREDENTIALS_FULL_URI / AWS_CONTAINER_AUTHORIZATION_TOKEN.
# Retry delays equal the TokenExchangeService 2.0.3 credentialRetryInSec
# defaults. Refresh margin, deny-by-default grants and per-component tokens
# are SuiFleet proposals.
# ---------------------------------------------------------------------------
CREDENTIAL_PATH = "/2016-11-01/credentialprovider/"
CREDENTIAL_URI = "http://127.0.0.1:{port}" + CREDENTIAL_PATH
REFRESH_MARGIN = 300
RETRY_SECONDS = {"client-error": 120, "server-error": 60, "unknown-error": 300}


class CredentialProvider:
    def __init__(self):
        self.grants: set = set()
        self.active: dict = {}  # component -> current token
        self.issued: set = set()
        self.upstream: list = []
        self.cache = None  # (credential_set, expiration)
        self.retry_at = None
        self.calls = 0
        self.now = 0

    def _revoke(self, component):
        self.active.pop(component, None)

    def _ok(self):
        return {"status": "ok", "credential_set": self.cache[0], "expiration": self.cache[1], "upstream_calls": self.calls}

    def _unavailable(self):
        return {"status": "unavailable", "retry_after": self.retry_at - self.now, "upstream_calls": self.calls}

    def apply(self, op: dict) -> dict:
        if "now" in op:
            if op["now"] < self.now:
                raise AssertionError("fixture time must not move backwards")
            self.now = op["now"]
        kind = op["op"]
        if kind == "grant":
            self.grants.add(op["component"])
            return {}
        if kind == "revoke":
            self.grants.discard(op["component"])
            self._revoke(op["component"])
            return {}
        if kind == "start":
            component, token = op["component"], op["token"]
            if token in self.issued:
                raise AssertionError("fixture tokens must be unique")
            self._revoke(component)
            if component not in self.grants:
                return {"env": {}}
            self.issued.add(token)
            self.active[component] = token
            return {"env": {"AWS_CONTAINER_CREDENTIALS_FULL_URI": CREDENTIAL_URI, "AWS_CONTAINER_AUTHORIZATION_TOKEN": token}}
        if kind == "stop":
            self._revoke(op["component"])
            return {}
        if kind == "upstream":
            self.upstream.append(op)
            return {}
        if kind != "request":
            raise AssertionError(f"unknown op {kind}")
        if op["method"] != "GET":
            return {"status": "method-not-allowed", "upstream_calls": self.calls}
        if op["path"] != CREDENTIAL_PATH:
            return {"status": "not-found", "upstream_calls": self.calls}
        token = op.get("authorization")
        if not isinstance(token, str) or token not in self.active.values():
            return {"status": "unauthorized", "upstream_calls": self.calls}
        valid = self.cache is not None and self.cache[1] > self.now
        if valid and self.cache[1] - self.now > REFRESH_MARGIN:
            return self._ok()
        if self.retry_at is not None and self.now < self.retry_at:
            return self._ok() if valid else self._unavailable()
        if not self.upstream:
            raise AssertionError("fixture must queue an upstream result before a refresh")
        result = self.upstream.pop(0)
        self.calls += 1
        if result["result"] == "ok":
            self.cache = (result["credential_set"], result["expiration"])
            self.retry_at = None
            return self._ok()
        self.retry_at = self.now + RETRY_SECONDS[result["result"]]
        return self._ok() if valid else self._unavailable()


# ---------------------------------------------------------------------------
# Catalog structure and cross-reference checks.
# ---------------------------------------------------------------------------
def _approved(root: Path, entry: dict, label: str, errors: list):
    approval = entry.get("approval", "pending")
    if approval not in ("pending", "approved"):
        errors.append(f"{label}: approval {approval!r}")
    if approval == "approved":
        ref = entry.get("decision_ref")
        if not ref or not (root / ref).exists():
            errors.append(f"{label}: approved decisions need an existing decision_ref")


def check_catalog(root: Path, catalog: dict, compat: dict, backlog_keys: set) -> list:
    errors = []
    if catalog.get("format_version") != 1:
        errors.append("managed catalog: format_version must be 1")
    if catalog.get("parity_claim") is not False:
        errors.append("managed catalog: parity_claim must be false")
    families = catalog.get("families", {})
    dispositions = catalog.get("dispositions", {})
    adapters = {a.get("id"): a for a in catalog.get("adapters", [])}
    if len(adapters) != len(catalog.get("adapters", [])):
        errors.append("managed catalog: duplicate adapter")
    rows = {}
    for row in catalog.get("components", []):
        name = row.get("name")
        label = f"component {name}"
        if name in rows:
            errors.append(f"{label}: duplicate")
        rows[name] = row
        if row.get("family") not in families:
            errors.append(f"{label}: unknown family")
        aws = row.get("aws", {})
        if aws.get("type") not in AWS_TYPES or not aws.get("os") or set(aws.get("os", [])) - AWS_OS:
            errors.append(f"{label}: aws type/os")
        disposition = row.get("disposition")
        if disposition not in dispositions:
            errors.append(f"{label}: disposition {disposition!r}")
        for ticket in row.get("tickets", []):
            if ticket not in backlog_keys:
                errors.append(f"{label}: unknown ticket {ticket}")
        if disposition in ("native", "native-component") and not row.get("tickets"):
            errors.append(f"{label}: {disposition} needs owning tickets")
        if not row.get("native_equivalent"):
            errors.append(f"{label}: needs native_equivalent guidance")
        if disposition == "adapter" and "adapter" not in row:
            errors.append(f"{label}: adapter disposition needs an adapter")
        if "adapter" in row and row["adapter"] not in adapters:
            errors.append(f"{label}: unknown adapter {row['adapter']}")
        elif disposition == "adapter" and "adapter" in row and name not in (adapters[row["adapter"]].get("replaces") or {}):
            errors.append(f"{label}: adapter {row['adapter']} does not list it in replaces")
        if "discontinued" in row and disposition not in ("exclude", "not-applicable"):
            errors.append(f"{label}: discontinued AWS services can only be excluded")
        _approved(root, row, label, errors)
    for family in catalog.get("required_families", []):
        if family not in families or not any(r.get("family") == family for r in rows.values()):
            errors.append(f"managed catalog: required family {family} has no component")
    platforms = {p["id"]: p for p in compat.get("platforms", [])}
    suites = compat.get("suites", {})
    for aid, adapter in adapters.items():
        label = f"adapter {aid}"
        if adapter.get("selection") not in SELECTIONS:
            errors.append(f"{label}: selection")
        for text in ADAPTER_TEXT:
            if not adapter.get(text):
                errors.append(f"{label}: needs {text}")
        if not adapter.get("aws_services"):
            errors.append(f"{label}: needs aws_services")
        replaces = adapter.get("replaces") or {}
        if not replaces:
            errors.append(f"{label}: replaces no AWS component")
        for name, version in replaces.items():
            row = rows.get(name)
            if row is None:
                errors.append(f"{label}: replaces unknown component {name}")
            elif row.get("adapter") != aid:
                errors.append(f"{label}: {name} does not reference this adapter")
            elif "reference_version" in row and version != row["reference_version"]:
                errors.append(f"{label}: {name} version {version} != reference {row['reference_version']}")
        if not adapter.get("platforms"):
            errors.append(f"{label}: needs platforms")
        for platform in adapter.get("platforms", []):
            if platform not in platforms or platforms[platform].get("native") is None:
                errors.append(f"{label}: platform {platform} has no native runtime")
        for ticket in adapter.get("depends_on", []):
            if ticket not in backlog_keys:
                errors.append(f"{label}: unknown ticket {ticket}")
        acceptance = adapter.get("acceptance", {})
        if not acceptance.get("runtime_tests") or not acceptance.get("environment"):
            errors.append(f"{label}: needs runtime_tests and environment")
        for suite in acceptance.get("reference_suites", []):
            if suite not in suites:
                errors.append(f"{label}: unknown suite {suite}")
        ticket = adapter.get("implementation_ticket")
        if ticket is not None and ticket not in backlog_keys:
            errors.append(f"{label}: unknown implementation ticket {ticket}")
        if adapter.get("selection") == "recommended" and ticket is None:
            request = adapter.get("ticket_request") or {}
            if not request.get("title") or any(t not in backlog_keys for t in request.get("depends_on", [])):
                errors.append(f"{label}: recommended adapters without a ticket need a valid ticket_request")
        if adapter.get("selection") == "deferred" and not adapter.get("selection_precondition"):
            errors.append(f"{label}: deferred adapters need a selection_precondition")
    decided = set()
    for decision in catalog.get("platform_decisions", []):
        pid = decision.get("platform")
        decided.add(pid)
        if pid not in platforms:
            errors.append(f"platform decision {pid}: unknown platform")
        if decision.get("recommendation") not in ("exclude", "support") or not decision.get("reason"):
            errors.append(f"platform decision {pid}: needs recommendation and reason")
        _approved(root, decision, f"platform decision {pid}", errors)
    for pid, platform in platforms.items():
        if platform.get("status") == "undecided" and pid not in decided:
            errors.append(f"platform {pid}: undecided without an SF-038 recommendation")
    return errors
