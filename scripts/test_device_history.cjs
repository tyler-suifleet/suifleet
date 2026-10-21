const assert = require('node:assert/strict');
const { loader } = require('./frontend_test_utils.cjs');
const { deviceUpdates, filterUpdates } = loader()('lib/deployment-history.ts');
const devices = [{ objectId: 'a', name: 'Alpha' }, { objectId: 'b', name: 'Beta' }, { objectId: 'c', name: 'Alpha' }];
const groups = [{ objectId: 'lab', name: 'Lab', deviceIds: ['a', 'b'] }, { objectId: 'prod', name: 'Production', deviceIds: ['b'] }];
const update = (id, time, extra = {}) => ({ deploymentId: id, timestampMs: time, componentName: 'app', version: '1.0.0', targetDevices: ['a'], resolvedDeviceIds: ['a'], targetGroups: [], receiptsVerified: true, statuses: { a: 1 }, ...extra });
const deployments = [update('success', 3), update('failure', 2, { statuses: { a: 2 } }), update('pending', 4, { targetDevices: ['b'], resolvedDeviceIds: ['b'], statuses: {} }),
  update('legacy', 1, { receiptsVerified: false, statuses: { a: 1, forged: 2 } }),
  update('duplicate-name', 5, { targetDevices: ['c'], resolvedDeviceIds: ['c'], statuses: { c: 1 } }),
  update('removed', 6, { targetDevices: ['gone'], resolvedDeviceIds: ['gone'], statuses: { gone: 2 } }),
  update('group-current', 7, { targetDevices: [], resolvedDeviceIds: ['b'], targetGroups: ['lab'], statuses: {} })];
const rows = deviceUpdates(deployments, devices, groups);
assert.equal(rows.length, 7);
assert.equal(rows.find(r => r.deployment.deploymentId === 'failure').outcome, 'Failed');
assert.equal(rows.find(r => r.deployment.deploymentId === 'legacy').outcome, 'Unverified');
assert.ok(!rows.some(r => r.deviceId === 'forged'), 'legacy status must not invent a device');
assert.equal(rows.find(r => r.deployment.deploymentId === 'pending').outcome, 'No receipt');
assert.equal(rows.find(r => r.deployment.deploymentId === 'group-current').outcome, 'No receipt', 'current membership is not installation proof');
assert.equal(rows.find(r => r.deviceId === 'gone').deviceName, 'Unavailable device');
assert.equal(rows.find(r => r.deviceId === 'b').currentGroups.length, 2);
assert.deepEqual(rows.find(r => r.deviceId === 'c').currentGroups, []);
assert.equal(deviceUpdates(deployments, devices)[0].currentGroups, null);
assert.equal(filterUpdates(rows, ' ALPHA ', '', 'name-asc').length, 4);
assert.equal(filterUpdates(rows, '', 'pRoDuCtIoN', 'name-asc').length, 2);
assert.ok(filterUpdates(rows, '', '', 'name-desc')[0].deviceName >= 'Beta');
assert.equal(filterUpdates(rows, '', '', 'newest')[0].deployment.deploymentId, 'group-current');
assert.ok(filterUpdates(rows, '', '', 'name-asc', 'a').every(r => r.deviceId === 'a'));
assert.ok(filterUpdates(rows, '', '', 'name-asc', '', 'prod').every(r => r.deviceId === 'b'));
assert.equal(filterUpdates(rows, 'missing', '', 'newest').length, 0);
assert.deepEqual(deployments[0].statuses, { a: 1 }, 'derivation must not modify receipt data');
console.log('PASS device history: verified outcomes, no legacy trust, no membership-as-success, multiple/no/unavailable groups, duplicate names, missing devices, searches and stable ID filters');
