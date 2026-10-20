#!/usr/bin/env node
// SF-037: translate an AWS Greengrass V2 recipe (JSON or YAML) into native
// catalog manifests with the dApp's translator, offline.
//
//   node scripts/greengrass_translate.cjs RECIPE --bind URI=BLOB_ID:SHA256 [--bind ...]
//
// Each artifact Uri is bound to its authenticated Walrus upload; s3:// URIs
// are never fetched. Prints the translation with migration guidance for every
// reason and reported difference. Exit 0 translated, 1 rejected, 2 usage.
const fs = require("node:fs");
const path = require("node:path");
const { loader } = require("./frontend_test_utils.cjs");
const translator = loader()(path.resolve(__dirname, "../dapp/src/lib/greengrass-recipe.ts"));

function usage(message) {
  process.stderr.write(`${message}\nusage: greengrass_translate.cjs RECIPE --bind URI=BLOB_ID:SHA256 [--bind ...]\n`);
  process.exit(2);
}

const args = process.argv.slice(2);
let file;
const bindings = {};
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--bind") {
    const value = args[++i] ?? usage("--bind needs a value");
    const at = value.lastIndexOf("=");
    const [blob, sha, extra] = value.slice(at + 1).split(":");
    if (at <= 0 || !/^[A-Za-z0-9_-]+$/.test(blob ?? "") || !/^[0-9a-f]{64}$/.test(sha ?? "") || extra !== undefined) usage(`invalid binding ${value}`);
    bindings[value.slice(0, at)] = { artifactBlobId: blob, artifactSha256: sha };
  } else if (file === undefined && !args[i].startsWith("--")) {
    file = args[i];
  } else {
    usage(`unexpected argument ${args[i]}`);
  }
}
if (file === undefined) usage("missing RECIPE");

const result = translator.translateGreengrassText(fs.readFileSync(file, "utf8"), bindings);
const codes = [...result.reasons, ...(result.informational ?? [])];
const guidance = Object.fromEntries(codes.map(code => [code, translator.GUIDANCE[code.startsWith("ComponentDependencies.") ? "aws-managed-dependency" : code]]));
const parsed = translator.parseGreengrassRecipe(fs.readFileSync(file, "utf8"));
const name = "recipe" in parsed && typeof parsed.recipe?.ComponentName === "string" ? parsed.recipe.ComponentName : "component";
const output = { ...result, guidance, ...(result.ipc_acl ? { ipc_acl_toml: translator.ipcAclToml(name, result.ipc_acl) } : {}) };
process.stdout.write(JSON.stringify(output, null, 2) + "\n");
process.exit(result.outcome === "translate" ? 0 : 1);
