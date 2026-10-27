const assert = require("node:assert/strict");
const { loader } = require("./frontend_test_utils.cjs");
const { deviceHealthTile, deploymentTile } = loader()("lib/dashboard-stats.ts");
const health = status => ({ status, codes: [], reasons: [], components: [] });
for (const data of [{}, { a: health("unavailable") }, { a: health("unreported") }, { a: health("unverified") }]) {
  assert.deepEqual(deviceHealthTile(["a"], data), { label: "Registered devices", value: 1, hint: "Health not reported" });
}
assert.deepEqual(deviceHealthTile(["a", "b", "c"], { a: health("healthy"), b: health("unreported"), outsider: health("healthy") }),
  { label: "Devices (healthy / total)", value: "1 / 3", hint: "2 without verified health" });
assert.equal(deviceHealthTile(["a"], { a: health("offline") }).value, "0 / 1");
assert.equal(deviceHealthTile(["a"], { a: health("degraded") }).value, "0 / 1");
assert.equal(deviceHealthTile([], {}).value, 0);
const control = { state: 0, not_before_ms: "0", expires_ms: "0", window_start_ms: "0", window_end_ms: "0", notice_ms: "0" };
const deployment = (changes = {}, extra = {}) => ({ control: { ...control, ...changes }, resolvedDeviceIds: ["a"], statuses: {}, ...extra });
const cases = [
  [deployment(), 1, 0],
  [deployment({ state: 1 }), 0, 1],
  [deployment({ state: 2 }), 0, 0],
  [deployment({ expires_ms: "100" }), 0, 0],
  [deployment({ not_before_ms: "101" }), 0, 0],
  [deployment({ window_start_ms: "101", window_end_ms: "200" }), 0, 0],
  [deployment({ window_start_ms: "50", window_end_ms: "100" }), 0, 0],
  [deployment({ window_start_ms: "100", window_end_ms: "200", notice_ms: "5" }), 1, 0],
  [deployment({}, { statuses: { a: 1 } }), 0, 0],
  [deployment({}, { statuses: { a: 2 } }), 0, 0],
  [deployment({}, { resolvedDeviceIds: [] }), 0, 0],
];
for (const [d, active, paused] of cases) {
  const tile = deploymentTile([d], 100);
  assert.equal(tile.active, active);
  assert.match(tile.hint, new RegExp(`^${paused} paused;`));
}
assert.equal(deploymentTile([deployment(), deployment({ state: 1 })], 100).active, 1);
console.log("PASS: dashboard unreported/partial health and admission counts, control time boundaries and completed targets");

// Render the actual dashboard to cover label/value/hint wiring as well as derivation.
const { appRequire } = require("./frontend_test_utils.cjs");
const React = appRequire("react");
const { renderToStaticMarkup } = appRequire("react-dom/server");
let reports = {};
const devices = [{ objectId: "a", name: "Never reported", ownerCapId: "cap" }];
const Dashboard = loader({
  "@/hooks/useDevices": { useDevices: () => ({ data: devices }) },
  "@/hooks/useDeployments": { useDeployments: () => ({ data: [
    deployment({}, { deploymentId: "active", timestampMs: 0, targetGroups: [], targetDevices: ["a"] }),
    deployment({ state: 1 }, { deploymentId: "paused", timestampMs: 0, targetGroups: [], targetDevices: ["a"] }),
  ] }) },
  "@/hooks/useGroups": { useGroups: () => ({ data: [] }) },
  "@/hooks/useFleetHealth": { useFleetHealth: () => ({ data: reports }), useNow: () => 100 },
  "@/hooks/useFleetAlerts": { useFleetAlerts: () => ({ status: "available", items: [] }) },
  "@/lib/alerts": { historicalFailures: () => 0 },
  "@/lib/device-services": { deviceHref: () => "/devices" },
  "@/components/ui": {
    PageHeader: () => null, StatusDot: () => null,
    Section: ({ children }) => React.createElement("section", null, children),
  },
  "next/link": { __esModule: true, default: ({ children, href, ...props }) => React.createElement("a", { href, ...props }, children) },
})("app/page.tsx").default;
let markup = renderToStaticMarkup(React.createElement(Dashboard));
assert.match(markup, /Registered devices/);
assert.match(markup, /Health not reported/);
assert.doesNotMatch(markup, /Devices \(healthy/);
assert.match(markup, /Active deployments<\/p><p[^>]*>1<\/p>/);
assert.match(markup, /1 paused;/);
reports = { a: health("healthy") };
markup = renderToStaticMarkup(React.createElement(Dashboard));
assert.match(markup, /Devices \(healthy \/ total\)/);
assert.match(markup, /1 \/ 1/);
console.log("PASS: actual dashboard renders registered/unreported, healthy/total and one admitting + one paused deployment");
