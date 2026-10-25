"""Separate Sui release compilation from live network submission (SF-056).

Two pinned CLIs, never interchangeable:

* BUILD compiles Move bytecode offline. It is the reviewed release compiler
  (Dockerfile SUI_VERSION, the SF-050 security baseline) and never talks to a
  network, so the network's protocol version does not constrain it.
* NETWORK builds, signs and submits transactions and runs the client-side
  upgrade compatibility check, which needs the network's protocol config. It
  must support the chain's current protocol version.

A CLI only implements protocol versions up to the MAX_PROTOCOL_VERSION in
crates/sui-protocol-config/src/lib.rs at its release tag (recorded below).
Sui 1.81.1 stops at 138; on a protocol-139 chain its `test-upgrade` fails
with "protocol version 139 is newer than the maximum version 138 supported
by this CLI", while publish and plain PTBs still succeed. That is why cached
fixtures kept passing while every fresh fixture failed after publishing.

A package transaction is built by NETWORK, then decoded and compared with
BUILD's output before signing: the exact modules, dependencies and upgrade
digest, sender, gas owner/budget and command shape. Only those verified bytes
are signed and executed. So changing PATH or upgrading NETWORK cannot
silently change the compiler, framework or fixture bytecode.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]

BUILD = {
    'role': 'build',
    'release': 'testnet-v1.81.1',
    'version': 'sui 1.81.1-e04cf9aa915c',
    'max_protocol': 138,
}
NETWORK = {
    'role': 'network',
    'release': 'testnet-v1.82.1',
    'version': 'sui 1.82.1-987123842623',
    'max_protocol': 139,
    'archive_sha256': '112bf204fab6927b135edfb316cddac07bdba754434b5f12d021ca7e8a72858c',
    'binary_sha256': 'de1d7effd53831af2863749befc35ec6df565b1fdfa8d8b2f5c866be23fa0d6b',
}
NETWORK_DEFAULT = '/opt/sui-network/sui'
PACKAGE_MODULE = '0x0000000000000000000000000000000000000000000000000000000000000002::package'


class Incompatible(RuntimeError):
    """A read-only preflight refused the toolchain before any write."""


class NotSubmitted(RuntimeError):
    """Verification refused a transaction before it was signed or submitted."""


def run_version(tool, run=subprocess.run):
    result = run([str(tool), '--version'], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise Incompatible(f'{tool} --version failed')
    return result.stdout.strip()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def resolve(tool):
    found = shutil.which(str(tool))
    if not found:
        raise Incompatible(f'{tool} not found')
    return str(Path(found).resolve())


def check_tools(build_tool, network_tool, run=subprocess.run, digest=sha256_file):
    """Exact pinned identities; offline. Returns provenance for evidence."""
    build_path, network_path = resolve(build_tool), resolve(network_tool)
    build_version, network_version = run_version(build_path, run), run_version(network_path, run)
    if build_version != BUILD['version']:
        raise Incompatible(f'build CLI is {build_version!r}; the pinned release compiler is {BUILD["version"]!r}')
    if network_version != NETWORK['version']:
        raise Incompatible(f'network CLI is {network_version!r}; the pinned transaction CLI is {NETWORK["version"]!r}')
    network_sha = digest(network_path)
    if network_sha != NETWORK['binary_sha256']:
        raise Incompatible('network CLI binary does not match the pinned release checksum')
    return {
        'build': dict(BUILD, path=build_path),
        'network': dict(NETWORK, path=network_path, sha256=network_sha),
    }


def preflight(tools, facts, expected_chain=None):
    """Read-only chain/protocol gate, run before any faucet request or write."""
    chain, protocol = facts.get('chain_identifier'), facts.get('protocol_version')
    if expected_chain is not None and chain != expected_chain:
        raise Incompatible(f'chain {chain} is not the expected {expected_chain}')
    if type(protocol) is not int or protocol <= 0:
        raise Incompatible('the node did not report a protocol version')
    if protocol > tools['network']['max_protocol']:
        raise Incompatible(f'network protocol {protocol} exceeds {tools["network"]["version"]} support '
                           f'({tools["network"]["max_protocol"]}); pin a newer transaction CLI')
    return {'chain': chain, 'epoch': facts.get('epoch'), 'protocol_version': protocol,
            'network_cli_max_protocol': tools['network']['max_protocol'],
            'build_cli_max_protocol': tools['build']['max_protocol'],
            'build_cli_offline': True}


def framework_pins(lockfile, environment='testnet'):
    """Framework sources Move.lock pins for an environment (provenance)."""
    pins, current = {}, None
    for line in Path(lockfile).read_text().splitlines():
        header = re.fullmatch(r'\[pinned\.([^.\]]+)\.([^\]]+)\]', line.strip())
        if header:
            current = header.group(2) if header.group(1) == environment else None
        elif current and line.startswith('source ='):
            pins[current] = line.partition('=')[2].strip()
    return pins


def compile_package(build_tool, package_dir, environment='testnet', run=subprocess.run):
    """Offline release compilation; Move.lock must not change."""
    lock = Path(package_dir) / 'Move.lock'
    before = lock.read_bytes() if lock.exists() else None
    result = run([str(build_tool), 'move', 'build', '--path', str(package_dir), '--dump-bytecode-as-base64',
                  '--build-env', environment, '--silence-warnings', '--no-tree-shaking'], capture_output=True, text=True, timeout=900)
    if result.returncode:
        raise RuntimeError(f'release compilation failed:\n{result.stderr[-3000:]}')
    if (lock.read_bytes() if lock.exists() else None) != before:
        raise RuntimeError('release compilation changed Move.lock')
    out = result.stdout
    built = json.loads(out[out.find('{'):])
    modules, dependencies, digest = built['modules'], built['dependencies'], built['digest']
    if not modules or len(digest) != 32:
        raise RuntimeError('release compilation returned no package')
    return {'modules': modules, 'dependencies': [normal(d) for d in dependencies], 'digest': list(digest)}


def normal(address):
    value = str(address).lower().removeprefix('0x')
    if not re.fullmatch(r'[0-9a-f]{1,64}', value):
        raise ValueError(f'invalid address {address!r}')
    return '0x' + value.rjust(64, '0')


def decode(tx_bytes, run=subprocess.run):
    """TransactionData decoded by the pinned frontend SDK."""
    result = run(['node', str(ROOT / 'scripts/sui_tx_inspect.cjs')], input=tx_bytes,
                 capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise NotSubmitted(f'transaction bytes are undecodable: {result.stderr.strip()[-500:]}')
    return json.loads(result.stdout)


def _pure(data, argument):
    if argument.get('kind') != 'Input':
        raise NotSubmitted('expected a transaction input')
    found = data['inputs'][argument['index']]
    if found.get('kind') != 'Pure':
        raise NotSubmitted('expected a pure input')
    return base64.b64decode(found['bytes'])


def _object(data, argument):
    if argument.get('kind') != 'Input':
        raise NotSubmitted('expected a transaction input')
    found = data['inputs'][argument['index']]
    if found.get('kind') != 'ImmOrOwnedObject':
        raise NotSubmitted('expected an owned object input')
    return normal(found['objectId'])


def verify_package_transaction(data, built, *, sender, budget, upgrade=None):
    """Exactly a publish (+ transfer of its UpgradeCap to sender) or an
    authorize/upgrade/commit triple carrying the pinned compiler's bytecode."""
    sender = normal(sender)
    if normal(data['sender']) != sender or normal(data['gas_owner']) != sender:
        raise NotSubmitted('transaction sender or gas owner is not the verified wallet')
    if int(data['gas_budget']) != int(budget):
        raise NotSubmitted('transaction gas budget differs from the reserved allowance')
    commands = data['commands']
    kinds = [c['kind'] for c in commands]
    package = commands[0] if upgrade is None else (commands[1] if len(commands) > 1 else {})
    if upgrade is None and kinds != ['Publish', 'TransferObjects']:
        raise NotSubmitted(f'unexpected publish commands {kinds}')
    if upgrade is not None and kinds != ['MoveCall', 'Upgrade', 'MoveCall']:
        raise NotSubmitted(f'unexpected upgrade commands {kinds}')
    if sorted(package['modules']) != sorted(built['modules']) or len(package['modules']) != len(built['modules']):
        raise NotSubmitted('transaction bytecode differs from the pinned release compiler output')
    if sorted(normal(d) for d in package['dependencies']) != sorted(built['dependencies']):
        raise NotSubmitted('transaction dependencies differ from the pinned release compiler output')
    if upgrade is None:
        transfer = commands[1]
        if transfer['objects'] != [{'kind': 'Result', 'index': 0}] or \
                _pure(data, transfer['address']) != bytes.fromhex(sender[2:]):
            raise NotSubmitted('publish does not transfer only its UpgradeCap to the wallet')
        return {'kind': 'publish', 'modules': len(built['modules'])}
    authorize, commit = commands[0], commands[2]
    if (normal(authorize['package']) + '::' + authorize['module'] != PACKAGE_MODULE
            or authorize['function'] != 'authorize_upgrade' or len(authorize['arguments']) != 3):
        raise NotSubmitted('upgrade is not authorized through 0x2::package::authorize_upgrade')
    if _object(data, authorize['arguments'][0]) != normal(upgrade['cap']):
        raise NotSubmitted('upgrade uses a different UpgradeCap')
    if _pure(data, authorize['arguments'][2]) != bytes([32, *built['digest']]):
        raise NotSubmitted('authorized digest differs from the pinned release compiler digest')
    if normal(package['package']) != normal(upgrade['package']) or package['ticket'] != {'kind': 'Result', 'index': 0}:
        raise NotSubmitted('upgrade targets a different package or ticket')
    if (normal(commit['package']) + '::' + commit['module'] != PACKAGE_MODULE or commit['function'] != 'commit_upgrade'
            or len(commit['arguments']) != 2 or _object(data, commit['arguments'][0]) != normal(upgrade['cap'])
            or commit['arguments'][1] != {'kind': 'Result', 'index': 1}):
        raise NotSubmitted('upgrade is not committed with its own receipt and UpgradeCap')
    return {'kind': 'upgrade', 'modules': len(built['modules']), 'policy': _pure(data, authorize['arguments'][1]).hex()}


def verify_ptb_transaction(data, *, sender, budget, calls):
    """An ordinary setup PTB from the verified wallet calling exactly `calls`."""
    sender = normal(sender)
    if normal(data['sender']) != sender or normal(data['gas_owner']) != sender:
        raise NotSubmitted('transaction sender or gas owner is not the verified wallet')
    if int(data['gas_budget']) != int(budget):
        raise NotSubmitted('transaction gas budget differs from the reserved allowance')
    if any(c['kind'] in ('Publish', 'Upgrade') for c in data['commands']):
        raise NotSubmitted('an ordinary setup transaction cannot publish or upgrade')
    actual = [f"{normal(c['package'])}::{c['module']}::{c['function']}" for c in data['commands'] if c['kind'] == 'MoveCall']
    expected = []
    for call in calls:
        package, _, rest = call.partition('::')
        expected.append(f'{normal(package)}::{rest}')
    if actual != expected:
        raise NotSubmitted(f'unexpected setup calls {actual}')
    return {'kind': 'ptb', 'calls': actual}


def identity(tx_bytes, data, budget):
    """The exact verified unsigned transaction, persisted before it is signed.
    Its digest is the only transaction this mutation can ever execute as."""
    expiration = data.get('expiration') or {}
    return {'tx_bytes': tx_bytes, 'digest': data['digest'], 'sender': normal(data['sender']),
            'gas_budget': int(budget),
            'gas_payment': [{'objectId': normal(p['objectId']), 'version': str(p['version'])}
                            for p in data.get('gas_payment') or []],
            'expiration': {'kind': expiration.get('kind', 'None'), 'max_epoch': expiration.get('max_epoch')}}


def _persist(persist, tx_bytes, data, budget):
    if persist is None:
        return
    try:
        persist(identity(tx_bytes, data, budget))
    except Exception as error:  # Never sign what could not be durably recorded.
        raise NotSubmitted(f'transaction identity was not persisted: {error}') from error


def _serialized(client, command, budget):
    serialized = client(*command, '--gas-budget', str(budget), '--serialize-unsigned-transaction',
                        json_output=False, check=False)
    tx_bytes = serialized.stdout.strip().splitlines()[-1].strip() if serialized.stdout.strip() else ''
    if serialized.returncode or not tx_bytes:
        raise NotSubmitted(f'{command[0]} could not build a transaction:\n{serialized.stderr[-3000:]}')
    return tx_bytes


def ptb_submitter(client, ptb, *, sender, calls, run=subprocess.run, inspect=decode, persist=None):
    """Callable for Budget.execute: build, verify and persist a setup PTB, then submit."""
    def submit(budget):
        tx_bytes = _serialized(client, ['ptb', *ptb], budget)
        data = inspect(tx_bytes, run=run)
        verify_ptb_transaction(data, sender=sender, budget=budget, calls=calls)
        _persist(persist, tx_bytes, data, budget)
        return client('serialized-tx', tx_bytes)
    return submit


def write_pubfile(path, chain, package_dir, package, upgrade_cap, version=1):
    """The ephemeral publication record test-upgrade reads. Serialized
    publication does not write it, so record exactly what the verified
    effects created."""
    Path(path).write_text(
        '# generated by scripts/sui_toolchain.py from verified publish effects\n'
        f'build-env = "testnet"\nchain-id = "{chain}"\n\n[[published]]\n'
        f'source = {{ local = {json.dumps(str(package_dir))} }}\n'
        f'published-at = "{normal(package)}"\noriginal-id = "{normal(package)}"\nversion = {int(version)}\n'
        f'toolchain-version = "{BUILD["version"].split()[1].split("-")[0]}"\n'
        'build-config = { flavor = "sui", edition = "2024" }\n'
        f'upgrade-capability = "{normal(upgrade_cap)}"\n')


def package_submitter(client, build_tool, package_dir, common, *, sender, upgrade=None, run=subprocess.run,
                      inspect=decode, record=None, persist=None):
    """Callable for Budget.execute: compile, build, verify, persist, then submit.
    `persist` durably records the verified transaction identity before signing."""
    def submit(budget):
        try:
            built = compile_package(build_tool, package_dir, run=run)
        except (RuntimeError, ValueError, KeyError) as error:
            raise NotSubmitted(str(error)) from error
        lock = Path(package_dir) / 'Move.lock'
        pinned = lock.read_bytes()
        command = ['test-publish', package_dir] if upgrade is None else \
            ['test-upgrade', package_dir, '--upgrade-capability', upgrade['cap']]
        try:
            tx_bytes = _serialized(client, [*command, *common], budget)
        finally:
            if lock.read_bytes() != pinned:
                raise NotSubmitted('network CLI changed the pinned Move.lock framework sources')
        data = inspect(tx_bytes, run=run)
        checked = verify_package_transaction(data, built, sender=sender, budget=budget, upgrade=upgrade)
        if record is not None:
            record.update(checked, digest=bytes(built['digest']).hex())
        _persist(persist, tx_bytes, data, budget)
        return client('serialized-tx', tx_bytes)
    return submit
