// Read-only gRPC facts for reconciling uncertain testnet mutations (SF-146).
// Never signs or submits. Usage:
//   node scripts/testnet_reconcile.cjs RPC transaction DIGEST
//   node scripts/testnet_reconcile.cjs RPC epoch
//   node scripts/testnet_reconcile.cjs RPC caps OWNER
//   node scripts/testnet_reconcile.cjs RPC coins OWNER
//   node scripts/testnet_reconcile.cjs RPC objects ID...
const path = require('node:path');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const req = createRequire(path.resolve(__dirname, '../dapp/package.json'));
const PAGES = 40; // Bounded scans; exceeding them is an explicit error, never a partial answer.

function created(effects, types) {
  return (effects.changedObjects || []).filter((o) => o.idOperation === 'Created')
    .map((o) => ({ objectId: o.objectId, type: (types || {})[o.objectId] || null, outputState: o.outputState }));
}

function commandFacts(command) {
  const body = command[command.$kind];
  const facts = { kind: command.$kind };
  if (command.$kind === 'Publish' || command.$kind === 'Upgrade') {
    facts.modules = body.modules;
    facts.dependencies = body.dependencies;
    if (command.$kind === 'Upgrade') facts.package = body.package;
  }
  if (command.$kind === 'MoveCall') facts.target = `${body.package}::${body.module}::${body.function}`;
  return facts;
}

async function transaction(client, digest) {
  let result;
  try {
    result = await client.getTransaction({ digest, include: { effects: true, transaction: true, objectTypes: true } });
  } catch (error) {
    if (error && error.constructor && error.constructor.name === 'TransactionError' && /not found/i.test(error.message)) {
      return { found: false, digest };
    }
    throw error;
  }
  const tx = result.Transaction || result.FailedTransaction;
  if (!tx || tx.digest !== digest || !tx.effects || !tx.transaction) throw Error('incomplete transaction facts');
  const data = tx.transaction;
  return {
    found: true, digest: tx.digest, success: tx.status.success === true,
    error: tx.status.success ? null : String(JSON.stringify(tx.status.error)).slice(0, 500),
    epoch: tx.epoch == null ? null : String(tx.epoch), checkpoint: tx.checkpoint == null ? null : String(tx.checkpoint),
    sender: data.sender, gas_owner: data.gasData.owner, gas_budget: String(data.gasData.budget),
    gas_used: tx.effects.gasUsed, commands: data.commands.map(commandFacts),
    created: created(tx.effects, tx.objectTypes),
  };
}

async function pages(list) {
  const items = [];
  let cursor = null;
  for (let page = 0; page < PAGES; page += 1) {
    const result = await list(cursor);
    items.push(...result.objects);
    if (!result.hasNextPage) return items;
    cursor = result.cursor;
  }
  throw Error('owned-object scan exceeded its page bound');
}

async function main() {
  const [rpc, command, ...args] = process.argv.slice(2);
  const { SuiGrpcClient } = await import(pathToFileURL(req.resolve('@mysten/sui/grpc')));
  const client = new SuiGrpcClient({ network: 'testnet', baseUrl: rpc });
  let output;
  if (command === 'transaction' && args.length === 1) {
    output = await transaction(client, args[0]);
  } else if (command === 'epoch' && !args.length) {
    const { systemState } = await client.getCurrentSystemState();
    output = { epoch: String(systemState.epoch) };
  } else if (command === 'caps' && args.length === 1) {
    const objects = await pages((cursor) => client.listOwnedObjects({
      owner: args[0], type: '0x2::package::UpgradeCap', cursor, include: { json: true, previousTransaction: true } }));
    output = objects.map((o) => ({ objectId: o.objectId, version: String(o.version), package: o.json && o.json.package,
      cap_version: o.json && String(o.json.version), previous_transaction: o.previousTransaction }));
  } else if (command === 'coins' && args.length === 1) {
    const objects = await pages((cursor) => client.listCoins({ owner: args[0], cursor }));
    output = objects.map((o) => ({ objectId: o.objectId, version: String(o.version) }));
  } else if (command === 'objects' && args.length) {
    const { objects } = await client.getObjects({ objectIds: args });
    output = objects.map((o, index) => (o instanceof Error
      ? { objectId: args[index], exists: false }
      : { objectId: o.objectId, exists: true, version: String(o.version), owner: o.owner }));
  } else {
    throw Error('usage: RPC transaction DIGEST | epoch | caps OWNER | coins OWNER | objects ID...');
  }
  process.stdout.write(JSON.stringify(output) + '\n');
}

main().catch((error) => { console.error(String(error && error.message || error).slice(0, 500)); process.exitCode = 1; });
