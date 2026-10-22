import React from "react";
import { bcs } from "@mysten/sui/bcs";
import { fromBase64 } from "@mysten/sui/utils";
import { WALRUS_PUBLISHER } from "../../dapp/src/lib/constants";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ComponentsPage from "../../dapp/src/app/components/page";
import FirmwarePage from "../../dapp/src/app/firmware/page";
export { buildCreateCatalogTx, buildPublishVersionTx, buildCatalogDeploymentTx } from "../../dapp/src/lib/catalog";

const query = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const client = {};
const accountListeners = new Set();
let address = location.search.includes("disconnected") ? null : "0xa";
export const useCurrentAccount = () => {
  const current = React.useSyncExternalStore(callback => { accountListeners.add(callback); return () => accountListeners.delete(callback); }, () => address);
  return current ? { address: current } : null;
};
export const useCurrentClient = () => client;
export const useAccessPolicies = () => ({ data: [] });
export const useDevices = () => ({ data: [{ objectId: "0x123", name: "Acceptance device", arch: "x86_64" }] });
export const useGroups = () => ({ data: [] });
const state = { catalogs: location.search.includes("noCatalog") ? [] : [{ objectId: "0xc" }], versions: [], requests: [], calls: [], error: false, delay: location.search.includes("loading") };
export async function fetchCatalogs() {
  if (state.delay) await new Promise(r => { state.loaded = r; });
  if (state.error) throw new Error("Library network unavailable");
  return state.catalogs;
}
export async function discoverVersions() { return state.versions; }
export async function fetchVersion() { return JSON.parse(sessionStorage.getItem("published")); }
export const useDeploymentStorage = () => ({ data: { recipe: "available", artifact: "available", confidential: false } });
export const useFleetTransaction = () => ({ isPending: false, mutateAsync: ({ transaction, onProgress }) => {
  state.calls.push(transaction.getData());
  state.onProgress = onProgress;
  onProgress?.("validating");
  return new Promise((resolve, reject) => { state.resolve = resolve; state.reject = reject; });
} });
class Request {
  upload = new EventTarget();
  open() {}
  setRequestHeader() {}
  send(body) { this.body = body; state.requests.push(this); }
}
window.XMLHttpRequest = Request;
const fetchOriginal = window.fetch.bind(window);
window.fetch = (url, options) => {
  if (!String(url).startsWith(`${WALRUS_PUBLISHER}/v1/blobs?`)) return fetchOriginal(url, options);
  return new Promise(resolve => state.requests.push({ body: new Blob([options.body]), finish: resolve }));
};
window.componentsAcceptance = {
  account(value) { address = value; for (const callback of accountListeners) callback(); },
  count: () => state.requests.length,
  calls: () => state.calls,
  progress(index, loaded, total) {
    const e = new Event("progress"); Object.assign(e, { loaded, total: total ?? 0, lengthComputable: total !== null });
    state.requests[index].upload.dispatchEvent(e);
  },
  finish(index, status = 200) {
    const r = state.requests[index]; r.status = status;
    r.responseText = JSON.stringify({ newlyCreated: { blobObject: { blobId: `blob-${index}`, storage: { endEpoch: 42 } } } });
    if (r.finish) r.finish(new Response(r.responseText, { status })); else r.onload();
  },
  phase: p => state.onProgress(p),
  reject: () => state.reject(new Error("User rejected signing")),
  succeed() {
    const data = state.calls.at(-1), args = data.commands[0].MoveCall.arguments;
    const pure = (i, schema) => schema.parse(fromBase64(data.inputs[args[i].Input].Pure.bytes));
    const strings = i => pure(i, bcs.vector(bcs.string()));
    const os = strings(3), arch = strings(4), blobs = strings(5), recipes = strings(6), artifacts = strings(7);
    const v = { objectId: "0x111", catalogId: "0xc", publisher: "0xa", name: pure(1, bcs.string()), version: pure(2, bcs.string()),
      manifests: os.map((os, i) => ({ os, arch: arch[i], recipeBlobId: blobs[i], recipeSha256: recipes[i], artifactSha256: artifacts[i] })) };
    state.versions = [v]; sessionStorage.setItem("published", JSON.stringify(v));
    state.resolve({ digest: "confirmed-fixture" });
  },
  async library(mode) {
    state.error = mode === "error"; state.delay = mode === "loading";
    state.catalogs = mode === "no-catalog" ? [] : [{ objectId: "0xc" }];
    state.versions = mode === "empty" || mode === "no-catalog" ? [] : state.versions;
    await query.invalidateQueries();
  },
  loaded() { state.delay = false; state.loaded?.(); },
  async recipe(index) { return JSON.parse(await state.requests[index].body.text()); },
};
const Page = location.pathname === "/firmware" ? FirmwarePage : ComponentsPage;
createRoot(document.getElementById("root")).render(<QueryClientProvider client={query}><Page /></QueryClientProvider>);
