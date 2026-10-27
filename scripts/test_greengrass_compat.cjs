// SF-037 golden-recipe conformance: executes the SF-028 recipe fixtures and the
// SF-038 managed-dependency fixtures against the real translator
// (dapp/src/lib/greengrass-recipe.ts), and checks every translation with the
// real frontend recipe validator. scripts/test_greengrass_runtime.py runs
// translated recipes on the real daemon.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { loader } = require("./frontend_test_utils.cjs");
const load = loader();
const recipes = load(path.resolve(__dirname, "../dapp/src/lib/recipe-schema.ts"));
const translator = load(path.resolve(__dirname, "../dapp/src/lib/greengrass-recipe.ts"));
const compat = path.resolve(__dirname, "../interfaces/compat");
const cases = JSON.parse(fs.readFileSync(path.join(compat, "aws-recipe-cases.json"), "utf8"));

function emptyRunRecipe(item) {
  const binding = item.bindings["s3://example-bucket/hello/1.0.0/hello.tar"];
  return { schemaVersion: "1.2", componentName: item.recipe.ComponentName, componentVersion: item.recipe.ComponentVersion,
    ...binding, lifecycle: item.recipe.Manifests[0].Lifecycle, configuration: { defaults: item.recipe.ComponentConfiguration.DefaultConfiguration } };
}

let translated = 0, texts = 0;
const codes = new Set();
for (const item of cases) {
  const expected = item.expected;
  let actual;
  if (item.recipe_text !== undefined) {
    texts++;
    const parsed = translator.parseGreengrassRecipe(item.recipe_text);
    if (item.recipe === undefined) {
      assert.deepEqual(parsed, expected, item.id);
    } else {
      assert.deepEqual(parsed, { recipe: item.recipe }, item.id);
    }
    actual = translator.translateGreengrassText(item.recipe_text, item.bindings ?? {});
  } else {
    actual = translator.translateGreengrassRecipe(item.recipe, item.bindings ?? {});
  }
  for (const key of Object.keys(expected)) assert.deepEqual(actual[key], expected[key], `${item.id}: ${key}`);
  for (const key of ["managed_dependencies", "ipc_acl"]) assert.equal(key in actual, key in expected, `${item.id}: ${key} must be pinned`);
  assert.equal(actual.outcome === "translate", Array.isArray(expected.manifests), item.id);
  // An AWS document is never a native recipe; every translation is one.
  if (item.recipe !== undefined) assert.throws(() => recipes.assertComponentRecipe(item.recipe), undefined, item.id);
  for (const manifest of actual.manifests ?? []) {
    assert.doesNotThrow(() => recipes.assertComponentRecipe(manifest.recipe), item.id);
    translated++;
  }
  for (const code of [...actual.reasons, ...(actual.informational ?? [])]) codes.add(code.startsWith("ComponentDependencies.") ? "aws-managed-dependency" : code);
}
for (const code of codes) assert.ok(translator.GUIDANCE[code], `no migration guidance for ${code}`);

const managed = JSON.parse(fs.readFileSync(path.join(compat, "managed-dependency-cases.json"), "utf8"));
for (const item of managed) assert.deepEqual(translator.classifyManagedDependency(item.dependency, item.spec), item.expected, item.id);

// The suggested operator ACL is TOML the daemon's [ipc] section accepts in shape.
const acl = translator.translateGreengrassRecipe(cases.find(c => c.id === "access-control-unmappable-reported").recipe, {}).ipc_acl;
assert.equal(translator.ipcAclToml("com.example.Hello", acl), [
  '[ipc.components."com.example.Hello"]', "uid = 0 # set to the component's dedicated non-root Unix UID",
  'operations = ["config", "health", "publish", "receive"]', 'publish = ["sensor/events"]',
  'subscribe = ["sensor/commands", "sensor/events"]'].join("\n"));

// What a translator copying the empty run would emit: both native validators
// (here and suifleet-edge/src/compat_tests.rs) reject it, so empty-step must.
const empty = cases.find(c => c.id === "empty-run-with-configuration-rejected");
assert.throws(() => recipes.assertComponentRecipe(emptyRunRecipe(empty)), /install or run command/);
// A __proto__ dependency survives serialization as an own key.
const proto = translator.translateGreengrassText(cases.find(c => c.id === "json-proto-dependency-kept").recipe_text, {
  "s3://example-bucket/hello/1.0.0/hello.tar": { artifactBlobId: "blob_hello", artifactSha256: "ab".repeat(32) } });
assert.deepEqual(Object.keys(JSON.parse(JSON.stringify(proto.manifests[0].recipe)).componentDependencies), ["__proto__"]);

// SF-167: the import panel offers a file input for every artifact URI in any
// letter case, so an AWS-cased recipe needs at most one binding round.
const awsCased = cases.find(c => c.id === "aws-cased-yaml-translates");
assert.deepEqual(translator.artifactUris(awsCased.recipe), ["s3://example-bucket/hello/1.0.0/hello-arm.tar", "s3://example-bucket/hello/1.0.0/hello.tar"]);
assert.deepEqual(translator.artifactUris(cases.find(c => c.id === "aws-cased-json-translates").recipe), ["s3://example-bucket/hello/1.0.0/hello.tar"]);
const everything = cases.find(c => c.id === "every-finding-in-one-check");
const bound = translator.translateGreengrassRecipe(everything.recipe, Object.fromEntries(translator.artifactUris(everything.recipe).map(uri => [uri, awsCased.bindings[uri]])));
assert.deepEqual(bound.reasons, ["configuration-variable", "unsupported-lifecycle-step"], "binding every listed URI resolves unbound-artifact in one round");

assert.ok(translated >= 10 && texts >= 10);
console.log(`PASS: translator executed ${cases.length} AWS recipe fixtures (${texts} JSON/YAML texts); ${translated} translations accepted by the frontend validator; guidance for ${codes.size} codes`);
console.log(`PASS: translator classified ${managed.length} SF-038 managed-dependency fixtures`);
