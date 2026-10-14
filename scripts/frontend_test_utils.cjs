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
const { bcs } = appRequire("@mysten/sui/bcs");
const { fromHex, toBase58 } = appRequire("@mysten/sui/utils");
function manifestClient(m) {
  const schemas = loader()("lib/move-bcs.ts");
  return {
    ledgerService: { getServiceInfo: async () => ({ response: { chainId: toBase58(fromHex(m.chain_identifier + "00".repeat(28))) } }) },
    getObject: async ({ objectId }) => {
      if (objectId === m.call_package_id) return { object: { objectBcs: bcs.Object.serialize({
        data: { Package: { id: m.call_package_id, version: m.package_version, moduleMap: new Map(), linkageTable: new Map(),
          typeOriginTable: Object.entries(m.type_origins).map(([name, origin]) => ({ moduleName: name.split("::")[0], datatypeName: name.split("::")[1], package: origin })) } },
        owner: { Immutable: true }, previousTransaction: toBase58(new Uint8Array(32)), storageRebate: 0,
      }).toBytes() } };
      if (objectId === m.upgrade_cap_id) return { object: { type: "0x2::package::UpgradeCap", content: schemas.UpgradeCapBcs.serialize({ id: objectId, package: m.call_package_id, version: m.package_version, policy: 0 }).toBytes() } };
      return { object: { type: `${m.type_origins["device_registry::DeviceRegistry"]}::device_registry::DeviceRegistry`, owner: { $kind: "Shared", Shared: { initialSharedVersion: "1" } } } };
    },
  };
}
module.exports = { loader, appRequire, manifestClient };
