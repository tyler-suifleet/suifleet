// Exercise the shipped search script without a network, browser or dependencies.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const script = fs.readFileSync(require("node:path").join(__dirname, "../assets/search.js"), "utf8");
function element() {
  return { value: "", textContent: "", children: [],
    replaceChildren() { this.children = []; },
    append(child) { this.children.push(child); },
    addEventListener(event, callback) { this.callback = callback; } };
}
async function run() {
  const elements = Object.fromEntries(["search", "search-results", "search-status"].map(id => [id, element()]));
  let calls = 0, fail = false;
  const context = {
    document: { getElementById: id => elements[id], createElement: element },
    fetch: async url => {
      assert.equal(url, "/search.json"); calls++;
      if (fail) throw new Error("offline");
      return { ok: true, json: async () => [
        { title: "Enrollment", path: "/enrollment/", text: "Provisioning media and keys" },
        { title: "Components", path: "/components/", text: "Deploy a version" },
      ] };
    },
  };
  vm.runInNewContext(script, context);
  const input = elements.search, results = elements["search-results"], status = elements["search-status"];
  input.value = "PROVISIONING keys"; await input.callback();
  assert.equal(results.children.length, 1);
  assert.equal(results.children[0].children[0].href, "/enrollment/");
  input.value = "<img src=x onerror=alert(1)>"; await input.callback();
  assert.equal(results.children.length, 0);
  assert.match(status.textContent, /No matching/);
  assert.equal(calls, 1);
  input.value = ""; await input.callback();
  assert.equal(status.textContent, "");
  // New page load with a failed fetch must offer navigation and allow retry.
  fail = true;
  vm.runInNewContext(script, {...context, document: context.document});
  input.value = "keys"; await input.callback();
  assert.match(status.textContent, /Search unavailable/);
  fail = false; await input.callback();
  assert.equal(results.children.length, 1);
}
run().then(() => console.log("search behavior passed")).catch(error => { console.error(error); process.exitCode = 1; });
