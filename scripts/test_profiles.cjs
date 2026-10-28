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
