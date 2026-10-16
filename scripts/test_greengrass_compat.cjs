// SF-028 current-API checks: the real frontend recipe validator rejects every
// AWS-format fixture and accepts every proposed native translation. This does
// not exercise an AWS recipe translator (SF-037), which does not exist yet.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { loader } = require("./frontend_test_utils.cjs");
const recipes = loader()(path.resolve(__dirname, "../dapp/src/lib/recipe-schema.ts"));
const cases = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../interfaces/compat/aws-recipe-cases.json"), "utf8"));
let translated = 0;
for (const item of cases) {
  assert.throws(() => recipes.assertComponentRecipe(item.recipe), undefined, item.id);
  for (const manifest of item.expected.manifests ?? []) {
    assert.doesNotThrow(() => recipes.assertComponentRecipe(manifest.recipe), item.id);
    translated++;
  }
  assert.equal(item.expected.outcome === "translate", Array.isArray(item.expected.manifests), item.id);
}
assert.ok(translated >= 5);
console.log(`PASS: frontend validator rejects ${cases.length} AWS recipe fixtures and accepts ${translated} proposed native translations`);
