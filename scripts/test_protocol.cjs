// Real generated SDK encoders and shared recipe compatibility cases.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const ts = require("../dapp/node_modules/typescript");
const cache = new Map();
function load(file) {
  const filename = path.resolve(__dirname, "..", file);
  if (cache.has(filename)) return cache.get(filename).exports;
  const loaded = new Module(filename, module);
  loaded.filename = filename;
  loaded.paths = Module._nodeModulePaths(path.resolve(__dirname, "../dapp"));
  cache.set(filename, loaded);
  const original = loaded.require.bind(loaded);
  loaded.require = (name) => {
    const candidate = path.resolve(path.dirname(filename), name + ".ts");
    return name.startsWith(".") && fs.existsSync(candidate)
      ? load(path.relative(path.resolve(__dirname, ".."), candidate)) : original(name);
  };
  loaded._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020,esModuleInterop:true}}).outputText, filename);
  return loaded.exports;
}
const protocol = load("dapp/src/lib/fleet-protocol.ts");
const recipes = load("dapp/src/lib/recipe-schema.ts");
const cases = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../interfaces/recipe-cases.json"), "utf8"));
for (const item of cases) {
  if (item.valid) assert.doesNotThrow(() => recipes.assertComponentRecipe(item.value), item.name);
  else assert.throws(() => recipes.assertComponentRecipe(item.value), undefined, item.name);
}
assert.doesNotThrow(() => recipes.assertComponentRecipe({...cases[0].value, description:undefined}));
const vector = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../interfaces/wire-vectors.json"), "utf8")).deployment_created;
assert.equal(Buffer.from(protocol.deployment_manager_DeploymentCreatedBcs.serialize(vector.json).toBytes()).toString("hex"), vector.bcs_hex);
// Supply an exact Uint8Array, not a Node pooled Buffer with unrelated backing bytes.
assert.deepEqual(protocol.deployment_manager_DeploymentCreatedBcs.parse(Uint8Array.from(Buffer.from(vector.bcs_hex, "hex"))), vector.json);
assert.equal(Buffer.from(protocol.deployment_manager_ReceiptsKeyBcs.serialize({dummy_field:false}).toBytes()).toString("hex"), "00");
const { Transaction } = require("../dapp/node_modules/@mysten/sui/transactions");
const tx = new Transaction();
protocol.calls.deployment_manager_create_deployment_verified(tx, "0x1", "app", "v1", "recipe", "ab".repeat(32), "cd".repeat(32), ["0x2"], ["0x3"], "0x6");
const data = tx.getData();
assert.equal(data.commands.length, 1);
assert.equal(data.commands[0].MoveCall.function, "create_deployment_verified");
assert.equal(data.commands[0].MoveCall.arguments.length, 8);
assert.equal(data.inputs.filter(i => i.$kind === "Pure").length, 7);
const pure = data.inputs.filter(i => i.$kind === "Pure").map(i => Buffer.from(i.Pure.bytes, "base64").toString("hex"));
const strings = ["app", "v1", "recipe", "ab".repeat(32), "cd".repeat(32)].map(value => Buffer.concat([Buffer.from([value.length]), Buffer.from(value)]).toString("hex"));
assert.deepEqual(pure, [...strings, "01" + "2".padStart(64, "0"), "01" + "3".padStart(64, "0")]);
assert.throws(() => protocol.calls.deployment_manager_report_status(new Transaction(), "0x1", "0x2", "0x3", 256));
console.log(`PASS: ${cases.length} shared recipe cases, independent BCS event/key vector and SDK call encoder`);
module.exports = { load };
