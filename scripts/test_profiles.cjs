const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const { readProfile, checkProfile } = require('./check_profiles.cjs');
const { manifestClient, loader } = require('./frontend_test_utils.cjs');
const { UpgradeCapBcs } = loader()('lib/move-bcs.ts');
const dir = path.resolve(__dirname, '../profiles/seal-testnet');
test('committed profile passes full application verification with matching chain facts', async () => {
  const result = await checkProfile(dir, manifestClient);
  assert.equal(result.version, '2');
});
test('upgrade without profile update fails with actionable stale-cap diagnostic', async () => {
  await assert.rejects(checkProfile(dir, m => {
    const client = manifestClient(m), get = client.getObject;
    client.getObject = async args => args.objectId === m.upgrade_cap_id ? { object: { type: '0x2::package::UpgradeCap', content: UpgradeCapBcs.serialize({ id: m.upgrade_cap_id, package: '0x123', version: '3', policy: 0 }).toBytes() } } : get(args);
    return client;
  }), /UpgradeCap does not identify latest call package\/version.*regenerate and commit/);
});
test('wrong chain fails', async () => {
  await assert.rejects(checkProfile(dir, m => manifestClient(m, () => '35834a8a')), /chain identifier mismatch/);
});
test('frontend environment cannot silently lag deployment file', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'sf186-'));
  try {
    const m = readProfile(dir);
    fs.writeFileSync(path.join(temp, 'deployment.json'), JSON.stringify(m));
    fs.writeFileSync(path.join(temp, 'frontend.env'), `NEXT_PUBLIC_DEPLOYMENT_MANIFEST='${JSON.stringify({...m, package_version: '1'})}'\n`);
    assert.throws(() => readProfile(temp), /frontend.env differs/);
  } finally { fs.rmSync(temp, { recursive: true }); }
});
test('incorrect introduction-package origins fail', async () => {
  await assert.rejects(checkProfile(dir, m => manifestClient({ ...m, type_origins: { ...m.type_origins, 'device_registry::DeviceRegistry': '0x123' } })), /type origin mismatch: device_registry::DeviceRegistry/);
});

test('transient fetch failure retries complete verification with fresh chain reads', async () => {
  let reads = 0;
  const delays = [], warnings = [];
  const result = await checkProfile(dir, m => {
    const client = manifestClient(m), get = client.ledgerService.getServiceInfo;
    client.ledgerService.getServiceInfo = async args => {
      reads++;
      if (reads < 3) throw new TypeError('fetch failed');
      return get(args);
    };
    return client;
  }, { sleep: async ms => delays.push(ms), warn: message => warnings.push(message) });
  assert.equal(result.chain, '4c78adac');
  assert.equal(reads, 3);
  assert.deepEqual(delays, [1000, 2000]);
  assert.equal(warnings.length, 2);
});

test('persistent transport outage fails after bounded attempts without stale-profile advice', async () => {
  let reads = 0;
  await assert.rejects(checkProfile(dir, m => {
    const client = manifestClient(m);
    client.ledgerService.getServiceInfo = async () => { reads++; throw new TypeError('fetch failed'); };
    return client;
  }, { sleep: async () => {}, warn: () => {} }), error => {
    assert.match(error.message, /transport unavailable after 3 attempts/);
    assert.doesNotMatch(error.message, /regenerate and commit/);
    return true;
  });
  assert.equal(reads, 3);
});

test('chain mismatch is never retried', async () => {
  let delays = 0;
  await assert.rejects(checkProfile(dir, m => manifestClient(m, () => '35834a8a'), {
    sleep: async () => { delays++; }, warn: () => assert.fail('mismatch must not retry'),
  }), /chain identifier mismatch/);
  assert.equal(delays, 0);
});

test('object transport failure restarts chain verification and preserves stale-cap rejection', async () => {
  let chainReads = 0, objectReads = 0;
  await assert.rejects(checkProfile(dir, m => {
    const client = manifestClient(m), info = client.ledgerService.getServiceInfo, get = client.getObject;
    client.ledgerService.getServiceInfo = async args => { chainReads++; return info(args); };
    client.getObject = async args => {
      if (args.objectId === m.upgrade_cap_id) {
        if (++objectReads === 1) throw new TypeError('fetch failed');
        return { object: { type: '0x2::package::UpgradeCap', content: UpgradeCapBcs.serialize({ id: m.upgrade_cap_id, package: '0x123', version: '3', policy: 0 }).toBytes() } };
      }
      return get(args);
    };
    return client;
  }, { sleep: async () => {}, warn: () => {} }), /UpgradeCap does not identify latest call package\/version/);
  assert.equal(chainReads, 2);
  assert.equal(objectReads, 2);
});
