// Fleet alerts (SF-020) in the dApp: strict parsing of the indexer's list,
// unavailable (never stale) results, owner scoping and the bearer token.
//   node scripts/test_alerts.cjs
const assert = require("node:assert/strict");
const { loader } = require("./frontend_test_utils.cjs");
const alerts = loader()("lib/alerts.ts");

const good = { key: "deployment-failed:0xd1:0xe1", kind: "deployment-failed", severity: "critical", device_id: "0xd1", device_name: "pi",
  deployment_id: "0xe1", component: "app", message: "app 2.0.0 failed on pi", action: "Search the device's logs.", since_ms: 5 };
const respond = (status, body) => async (url, init) => {
  respond.calls.push({ url, init });
  return { ok: status >= 200 && status < 300, status, json: async () => body };
};
respond.calls = [];

(async () => {
  const parsed = alerts.parseAlert(good);
  assert.equal(parsed.deviceName, "pi");
  assert.equal(parsed.sinceMs, 5);
  for (const [label, bad] of [["unknown kind", { ...good, kind: "rm-rf" }], ["severity", { ...good, severity: "info" }],
    ["long message", { ...good, message: "x".repeat(401) }], ["missing device", { ...good, device_id: undefined }], ["since", { ...good, since_ms: -1 }]]) {
    assert.throws(() => alerts.parseAlert(bad), undefined, label);
  }
  // Service-health alerts (SF-085) are known kinds.
  for (const kind of ["messaging-backpressure", "messaging-loss", "bridge-disconnected", "stream-near-limit", "stream-data-lost",
    "export-failing", "shadow-sync-failing", "shadow-conflict"]) {
    assert.equal(alerts.parseAlert({ ...good, kind, key: `${kind}:0xd1:metrics`, deployment_id: undefined, component: undefined }).kind, kind);
  }
  const ok = await alerts.fetchAlerts("0xowner", "https://fleet.example/api/", "tok", respond(200, { items: [good], server_time_ms: 9 }));
  assert.equal(ok.status, "ok");
  assert.equal(ok.items.length, 1);
  assert.equal(respond.calls[0].url, "https://fleet.example/api/v1/alerts?owner=0xowner");
  assert.equal(respond.calls[0].init.headers.authorization, "Bearer tok");
  const unconfigured = await alerts.fetchAlerts("0xowner", "", "tok", respond(200, {}));
  assert.equal(unconfigured.status, "unavailable", "no indexer configured");
  assert.doesNotMatch(unconfigured.reason, /NEXT_PUBLIC_|docs\/|SF-\d{3}/, "customer-facing reason");
  assert.equal((await alerts.fetchAlerts("0xowner", "https://x", "", respond(401, {}))).status, "unavailable");
  assert.equal((await alerts.fetchAlerts("0xowner", "https://x", "", respond(200, { items: [{ ...good, kind: "bogus" }], server_time_ms: 1 }))).status, "unavailable",
    "one invalid alert makes the list unavailable, not partially shown");
  assert.equal((await alerts.fetchAlerts("0xowner", "https://x", "", async () => { throw new Error("offline"); })).status, "unavailable");
  // Stale or failed refreshes never keep an old list on screen.
  assert.equal(alerts.currentAlerts(ok, 1000, false, 1000 + alerts.ALERTS_STALE_MS - 1).status, "ok");
  assert.equal(alerts.currentAlerts(ok, 1000, false, 1000 + alerts.ALERTS_STALE_MS + 1).status, "unavailable");
  assert.equal(alerts.currentAlerts(ok, 1000, true, 1000).status, "unavailable");
  // Historical failed results (SF-098) count every failed device receipt, recovered or not.
  assert.equal(alerts.historicalFailures([]), 0);
  assert.equal(alerts.historicalFailures([{ statuses: { "0xd1": 2, "0xd2": 1 } }, { statuses: { "0xd1": 1 } }, { statuses: { "0xd1": 2, "0xd3": 2 } }]), 3);
  console.log("PASS: dApp alerts: strict parsing (service-health kinds included), owner-scoped authenticated fetch, unavailable instead of stale or partial lists, historical failures counted apart");
})().catch((error) => { console.error(error); process.exit(1); });
