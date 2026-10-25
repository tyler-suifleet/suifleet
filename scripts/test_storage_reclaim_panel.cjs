// SF-129 reclaim panels: a dry-run preview authorizes signing only the plan,
// context, account and network it simulated. Runs the actual component source
// with a stateful hook runtime; chain, wallet and planner calls are injected.
const fs = require('node:fs'), path = require('node:path'), Module = require('node:module'), assert = require('node:assert/strict');
const { appRequire, loader } = require('./frontend_test_utils.cjs');
const ts = appRequire('typescript');

// Minimal React hook runtime: state and refs persist across renders by call order.
let slots = [], cursor = 0;
const react = {
  useState(initial) {
    const i = cursor++;
    if (!(i in slots)) slots[i] = { value: initial };
    const slot = slots[i];
    return [slot.value, (next) => { slot.value = typeof next === 'function' ? next(slot.value) : next; }];
  },
  useRef(initial) { const i = cursor++; if (!(i in slots)) slots[i] = { current: initial }; return slots[i]; },
};
const env = { account: { address: '0xa1' }, client: { name: 'testnet' } };
const simulated = [], signed = [], plans = [];
let pendingPreview;
const reclaimLib = {
  formatReclaim: (mist) => `${mist} MIST`, netReclaim: () => ({ netMist: 1n }),
  async planArchiveReclaim(_client, _config, _origin, _devices, epoch, _limit, resume) {
    const plan = { epoch, label: resume ? 'B' : 'A', anchors: [{}], loose: [], scanned: 1, truncated: !resume, resume: resume ? {} : { d: 'c' } };
    plans.push(plan.label); return plan;
  },
  buildArchiveReclaimTx: (_config, capId, _system, plan) => ({ tx: { label: `${plan.label}:${capId}`, setSender() {} } }),
  async planDeploymentReclaim() { return [{ eligible: true, reason: 'cancelled', deployment: { deploymentId: `0x${plans.push('D')}`, componentName: 'c', version: String(plans.length) } }]; },
  buildRetireDeploymentsTx: (_pkg, records) => ({ label: records.map((r) => r.deployment.version).join(), setSender() {} }),
  previewReclaim(_client, tx) {
    simulated.push(tx.label);
    return new Promise((resolve) => { pendingPreview = () => resolve({ ok: true, netMist: 5n, rebateMist: 6n, computationMist: 1n, storageCostMist: 0n }); });
  },
};
const dependencies = {
  react,
  '@mysten/dapp-kit-react': { useCurrentAccount: () => env.account, useCurrentClient: () => env.client },
  '@tanstack/react-query': { useQueryClient: () => ({ invalidateQueries: async () => {} }) },
  '@/hooks/useFleetTransaction': { useFleetTransaction: () => ({ isPending: false, async mutateAsync({ transaction }) { signed.push(transaction.label); return { digest: 'D1' }; } }) },
  '@/lib/constants': { PACKAGE_ID: '0x82' },
  '@/lib/fleet-data': { DATA_CONFIG: {}, async listFleetDeviceIds() { return { devices: ['0xd1'], hasNextPage: false }; } },
  '@/lib/storage-reclaim': reclaimLib,
  '@/lib/stream-retention': loader()('lib/stream-retention.ts'),
};
const filename = path.resolve(__dirname, '../dapp/src/components/StorageReclaim.tsx');
const loaded = new Module(filename, module);
loaded.filename = filename;
loaded.require = (name) => Object.hasOwn(dependencies, name) ? dependencies[name] : appRequire(name);
loaded._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText, filename);
const panels = loaded.exports;
env.client.waitForTransaction = async () => ({ Transaction: { effects: { gasUsed: { computationCost: '1', storageCost: '0', storageRebate: '6' } } } });

/** Render a panel and return its element tree's buttons and text. */
function mount(component, props) {
  slots = [];
  const view = () => {
    cursor = 0;
    const buttons = {}, text = [];
    (function walk(node) {
      if (node == null || typeof node === 'boolean') return;
      if (Array.isArray(node)) return node.forEach(walk);
      if (typeof node !== 'object') return void text.push(String(node));
      if (typeof node.type === 'function') return walk(node.type(node.props));
      const children = node.props?.children;
      if (node.type === 'button') buttons[[children].flat().join('')] = node.props;
      walk(children);
    })(component(props));
    return { buttons, text: text.join('') };
  };
  return view;
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
async function click(view, label) {
  const button = view().buttons[label];
  assert.ok(button, `${label} is rendered`);
  assert.ok(!button.disabled, `${label} is enabled`);
  await button.onClick(); await flush();
}

(async () => {
  // Dry-run plan A, continue the scan to plan B: A's preview must not sign B.
  {
    const view = mount(panels.ArchiveReclaimPanel, { config: { walrus: { system: '0x5e' }, packageId: '0x5a' }, capId: '0xc1', epoch: 600 });
    await click(view, 'Find expired archives');
    const dryRun = view().buttons['Dry run'].onClick(); pendingPreview(); await dryRun; await flush();
    assert.ok(!view().buttons['Sign and reclaim'].disabled, 'a successful preview of the current plan enables signing');
    assert.match(view().text, /Dry run: 5 MIST recoverable/);
    await click(view, 'Continue scan');
    assert.deepEqual(plans, ['A', 'B']);
    assert.ok(view().buttons['Sign and reclaim'].disabled, 'continuing the scan invalidates the earlier preview');
    assert.doesNotMatch(view().text, /Dry run: /);
    const stale = view().buttons['Sign and reclaim'];
    await stale.onClick(); await flush();
    assert.deepEqual(signed, [], 'signing without a current preview submits nothing');
    assert.match(view().text, /successful dry run of the current plan/);
    // A slow dry run of B, overtaken by a rescan to a new plan A, is discarded.
    const late = view().buttons['Dry run'].onClick();
    await click(view, 'Find expired archives');
    pendingPreview(); await late; await flush();
    assert.ok(view().buttons['Sign and reclaim'].disabled, 'a late simulation of an obsolete plan never enables signing');
    // The current plan, previewed, signs exactly what was simulated.
    const current = view().buttons['Dry run'].onClick(); pendingPreview(); await current; await flush();
    await click(view, 'Sign and reclaim');
    assert.deepEqual(simulated, ['A:0xc1', 'B:0xc1', 'A:0xc1']);
    assert.deepEqual(signed, ['A:0xc1']);
    assert.match(view().text, /Confirmed D1/);
    assert.ok(!view().buttons['Sign and reclaim'] || view().buttons['Sign and reclaim'].disabled, 'a confirmed reclaim consumes its preview');
    // Switching the wallet account hides a preview made for another sender.
    const again = view().buttons['Dry run'].onClick(); pendingPreview(); await again; await flush();
    assert.ok(!view().buttons['Sign and reclaim'].disabled);
    env.account = { address: '0xa2' };
    assert.ok(view().buttons['Sign and reclaim'].disabled, 'an account change invalidates the preview');
    env.account = { address: '0xa1' };
  }
  // Deployment records: rescanning replaces the plan and discards its preview.
  {
    signed.length = 0;
    const view = mount(panels.DeploymentReclaimPanel, { deployments: [] });
    await click(view, 'Find finished records');
    const dryRun = view().buttons['Dry run'].onClick(); pendingPreview(); await dryRun; await flush();
    assert.ok(!view().buttons['Sign and reclaim'].disabled);
    await click(view, 'Find finished records');
    assert.ok(view().buttons['Sign and reclaim'].disabled, 'rescanning invalidates the earlier preview');
    const next = view().buttons['Dry run'].onClick(); pendingPreview(); await next; await flush();
    await click(view, 'Sign and reclaim');
    assert.equal(signed.length, 1);
    assert.equal(signed[0], simulated.at(-1), 'the signed records are the ones previewed');
  }
  console.log('PASS: SF-129 reclaim panels bind each dry-run preview to its plan, account and network; rescans, continued scans and late simulations never authorize signing');
})().catch((error) => { console.error(error); process.exitCode = 1; });
