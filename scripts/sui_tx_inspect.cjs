// Decode base64 TransactionData (stdin) with the pinned frontend SDK and print
// the fields scripts/sui_toolchain.py verifies before signing. Read-only.
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const req = createRequire(path.resolve(__dirname, '../dapp/package.json'));

function argument(value) {
  switch (value.$kind) {
    case 'Input': return { kind: 'Input', index: value.Input };
    case 'Result': return { kind: 'Result', index: value.Result };
    case 'NestedResult': return { kind: 'NestedResult', index: value.NestedResult[0], result: value.NestedResult[1] };
    default: return { kind: value.$kind };
  }
}

function input(value) {
  if (value.$kind === 'Pure') return { kind: 'Pure', bytes: value.Pure.bytes };
  if (value.$kind === 'Object') {
    const kind = value.Object.$kind;
    const object = value.Object[kind];
    return { kind, objectId: object.objectId };
  }
  return { kind: value.$kind };
}

function command(value) {
  const kind = value.$kind;
  const body = value[kind];
  switch (kind) {
    case 'MoveCall':
      return { kind, package: body.package, module: body.module, function: body.function,
        typeArguments: body.typeArguments, arguments: body.arguments.map(argument) };
    case 'Publish':
      return { kind, modules: body.modules, dependencies: body.dependencies };
    case 'Upgrade':
      return { kind, modules: body.modules, dependencies: body.dependencies, package: body.package,
        ticket: argument(body.ticket) };
    case 'TransferObjects':
      return { kind, objects: body.objects.map(argument), address: argument(body.address) };
    default:
      return { kind };
  }
}

async function main() {
  const { Transaction } = await import(pathToFileURL(req.resolve('@mysten/sui/transactions')));
  const data = Transaction.from(fs.readFileSync(0, 'utf8').trim()).getData();
  process.stdout.write(JSON.stringify({
    sender: data.sender,
    gas_owner: data.gasData.owner,
    gas_budget: String(data.gasData.budget),
    inputs: data.inputs.map(input),
    commands: data.commands.map(command),
  }) + '\n');
}

main().catch((error) => { console.error(String(error && error.message || error)); process.exitCode = 1; });
