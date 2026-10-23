// Local support console (SF-079) in a real browser, against the real daemon
// started by scripts/test_console.py --browser:
//   ui      sign in, health, inventory, redacted logs, restart, sign out;
//           no CSP violation, page error or script-readable session cookie
//   attack  a signed-in browser visits a page on another loopback port that
//           tries a simple cross-origin POST, a JSON fetch (preflight) and a
//           form submission against the API; Python then checks they failed
//   node scripts/test_console_browser.cjs ui|attack CONSOLE_URL TOKEN
const http = require("node:http");
const assert = require("node:assert/strict");
const { appRequire } = require("./frontend_test_utils.cjs");
const { chromium } = appRequire("playwright");

(async () => {
  const [mode, url, token] = process.argv.slice(2);
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ["--no-sandbox"] });
  let attacker;
  try {
    const page = await browser.newPage();
    const problems = [];
    page.on("console", (m) => { if (m.type() === "error") problems.push(m.text()); });
    page.on("pageerror", (e) => problems.push(String(e)));
    const status = (text) => page.waitForFunction((t) => document.getElementById("status").textContent === t, text, { timeout: 15000 });
    await page.goto(url);
    await page.fill("#token", token);
    await page.click("#login button[type=submit]");
    await status("Signed in.");
    assert.equal(await page.inputValue("#token"), "", "token left in the form");
    assert.equal(await page.evaluate(() => document.cookie), "", "session cookie readable by script");
    if (mode === "ui") {
      await page.click("[data-op=health]");
      await status("health: ok");
      assert.match(await page.textContent("#output"), /"health": "(healthy|degraded)"/);
      await page.click("[data-op=inventory]");
      await status("inventory: ok");
      await page.click("td.link >> text=svc");
      assert.equal(await page.textContent("#component-name"), "svc");
      await page.click("[data-component-op=logs]");
      await status("logs: ok");
      const logs = await page.textContent("#output");
      assert.match(logs, /password=\[redacted\]/);
      assert.ok(!logs.includes("hunter2-log-secret") && !logs.includes(token), logs);
      await page.click("[data-component-op=restart]");
      await status("restart: ok");
      await page.click("#logout");
      await status("Signed out.");
      assert.equal(await page.isVisible("#console"), false);
      assert.deepEqual(problems, []);
      console.log(JSON.stringify({ mode, ok: true }));
      return;
    }
    assert.equal(mode, "attack");
    // Another origin on the same host: same-site for cookies, so only the
    // console's Origin/Content-Type/CSRF checks stand between it and the API.
    const target = `${url}api/v1/request`;
    const body = JSON.stringify({ version: 1, operation: "restart", component: "svc" });
    const html = `<!doctype html><form id=f method=POST action="${target}" enctype="text/plain">
      <input name='${body.slice(0, -1)},"x":"' value='"}'></form><script>
      (async () => {
        const results = {};
        try { await fetch(${JSON.stringify(target)}, { method: "POST", mode: "no-cors", credentials: "include",
          headers: { "Content-Type": "text/plain" }, body: ${JSON.stringify(body)} }); results.simple = "sent"; }
        catch (e) { results.simple = "blocked"; }
        try { const r = await fetch(${JSON.stringify(target)}, { method: "POST", credentials: "include",
          headers: { "Content-Type": "application/json", "X-SuiFleet-CSRF": "guess" }, body: ${JSON.stringify(body)} });
          results.json = "read:" + r.status; }
        catch (e) { results.json = "blocked"; }
        document.title = JSON.stringify(results);
        document.getElementById("f").submit();
      })();</script>`;
    attacker = http.createServer((req, res) => { res.writeHead(200, { "Content-Type": "text/html" }); res.end(html); });
    await new Promise((resolve) => attacker.listen(0, "127.0.0.1", resolve));
    const evil = await browser.newPage();
    await evil.goto(`http://127.0.0.1:${attacker.address().port}/`);
    await evil.waitForURL(target, { timeout: 15000 });
    const form = JSON.parse(await evil.textContent("body"));
    // The same-site cookie arrives without its CSRF token, from a foreign Origin.
    assert.ok(["unauthenticated", "invalid-origin"].includes(form.status), form.status);
    console.log(JSON.stringify({ mode, ok: true, form: form.status }));
  } finally {
    if (attacker) attacker.close();
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exit(1); });
