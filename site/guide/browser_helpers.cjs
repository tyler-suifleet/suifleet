function helpers(page) {
  const caption = (text) => page.evaluate((t) => window.__guideCaption?.(t), text);
  const pause = (ms = 900) => page.waitForTimeout(ms);
  async function click(locator) {
    await locator.scrollIntoViewIfNeeded();
    await locator.evaluate(el => el.scrollIntoView({ block: "center", inline: "nearest" }));
    const box = await locator.boundingBox();
    if (box) await page.evaluate(([x, y]) => window.__guideMark?.(x, y), [box.x + box.width / 2, box.y + box.height / 2]);
    await pause(450);
    await locator.click();
  }
  async function type(locator, text, delay = 35) {
    await click(locator);
    // Controlled amount fields may restore their estimate when emptied. Set
    // the complete value in one input event, as a user pasting/select-replacing.
    await locator.fill(text);
    await pause(400);
  }
  const sidebar = (label) => page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: label, exact: true }).first();
  return { caption, pause, click, type, sidebar };
}

async function reconnect(page) {
  const connect = page.getByRole("button", { name: "Connect Wallet", exact: true }).first();
  // Locators pierce the wallet UI's shadow root. DOM queries do not.
  await connect.or(page.locator("#fleet-switcher")).first().waitFor({ timeout: 60000 });
  if (await connect.isVisible()) {
    await connect.click();
    await page.getByRole("button", { name: /SuiFleet Guide Wallet/ }).first().click();
  }
  await page.waitForFunction(() => document.querySelector("#fleet-switcher")?.value, null, { timeout: 60000 });
}
function assertMutationLease(token, execute = require("node:child_process").execFileSync) {
  try {
    if (!token) throw new Error("missing lease");
    execute("ticket", ["--project", "suifleet", "lock-assert", "network:suifleet-testnet", "--lock-token", token], { stdio: "ignore" });
  } catch {
    // Exceptions include argv; never display the token in a wallet rejection.
    throw new Error("testnet mutation lease refused");
  }
}
module.exports = { helpers, reconnect, assertMutationLease };
