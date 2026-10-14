// Compile real application modules with TypeScript; keep SDK implementations real.
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const ts = require("../dapp/node_modules/typescript");
const appRequire = Module.createRequire(path.resolve(__dirname, "../dapp/package.json"));
function loader(dependencies = {}) {
  const cache = new Map();
  function load(filename) {
    filename = path.resolve(filename);
    if (cache.has(filename)) return cache.get(filename).exports;
    const loaded = new Module(filename, module);
    loaded.filename = filename;
    loaded.paths = Module._nodeModulePaths(path.resolve(__dirname, "../dapp"));
    cache.set(filename, loaded);
    loaded.require = (name) => {
      if (Object.hasOwn(dependencies, name)) return dependencies[name];
      const candidate = name.startsWith("@/") ? path.resolve(__dirname, "../dapp/src", name.slice(2)) :
        name.startsWith(".") ? path.resolve(path.dirname(filename), name) : undefined;
      if (candidate) {
        for (const ext of [".ts", ".tsx"]) if (fs.existsSync(candidate + ext)) return load(candidate + ext);
        if (fs.existsSync(candidate)) return appRequire(candidate);
      }
      return appRequire(name);
    };
    loaded._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true, resolveJsonModule: true },
    }).outputText, filename);
    return loaded.exports;
  }
  return (name) => load(path.resolve(__dirname, "../dapp/src", name));
}
const { manifestClient } = loader()("../../scripts/fixtures/manifest-client.ts");
module.exports = { loader, appRequire, manifestClient };
