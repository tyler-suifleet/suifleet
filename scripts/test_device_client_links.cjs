// The console links resolve to rendered public documentation; commands shown in-app exist
// in the daemon, and Get started, Devices and Enrollment all show the help.
// Offline by default; --online checks served pages and anchors after publication.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { loader, appRequire } = require('./frontend_test_utils.cjs');

const root = path.resolve(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(root, file), 'utf8');
const client = loader()('lib/device-client.ts');
const { DOCS_BASE, GUIDE_FILE, GUIDE_SECTIONS, COMMANDS, ADDRESS_LINE, PLATFORMS, guideUrl } = client;

// Public slugs are curated independently of source filenames. The docs Python
// regression checks these sections against actual rendered HTML.
const pages = JSON.parse(read('site/docs/pages.json')).pages;
const page = pages.find((page) => `${page.slug}/` === GUIDE_FILE);
assert.equal(page?.source, 'docs/INSTALL_DEVICE_CLIENT.md');
const docPath = (url) => {
  assert.ok(url.startsWith(DOCS_BASE), `${url}: not the configured public docs root`);
  const [file, anchor] = url.slice(DOCS_BASE.length).split('#');
  assert.equal(file, GUIDE_FILE);
  if (anchor) assert.ok(Object.values(GUIDE_SECTIONS).includes(anchor), url);
  return file;
};
const urls = [guideUrl(), ...Object.keys(GUIDE_SECTIONS).map((section) => guideUrl(section))];
for (const url of urls) docPath(url);
for (const platform of PLATFORMS) assert.ok(Object.hasOwn(GUIDE_SECTIONS, platform.section));
const previous = process.env.NEXT_PUBLIC_DEVICE_DOCS_BASE;
process.env.NEXT_PUBLIC_DEVICE_DOCS_BASE = 'https://customer.example/help';
assert.equal(loader()('lib/device-client.ts').guideUrl('debian'), 'https://customer.example/help/device-client/#debian-and-ubuntu');
if (previous === undefined) delete process.env.NEXT_PUBLIC_DEVICE_DOCS_BASE;
else process.env.NEXT_PUBLIC_DEVICE_DOCS_BASE = previous;
console.log(`PASS: ${urls.length} console links resolve to curated public docs; custom docs roots supported`);

const guidePath = 'docs/INSTALL_DEVICE_CLIENT.md', guide = read(guidePath);
// 3. Commands and the printed address line match the client and its installers.
const usage = /"usage: suifleet \[([^"]+)\]"/.exec(read('suifleet-edge/src/linux_main.rs'));
assert.ok(usage, 'daemon usage string not found');
for (const command of Object.values(COMMANDS)) {
  for (const flag of command.match(/--[a-z-]+/g) ?? []) {
    if (command.startsWith('sudo suifleet')) assert.ok(usage[1].includes(flag), `daemon has no ${flag}`);
  }
  assert.ok(guide.includes(command), `guide does not show "${command}"`);
}
const prefix = ADDRESS_LINE.replace(/0x…$/, '');
assert.equal(prefix, 'SuiFleet device address: ');
for (const installer of ['suifleet-edge/meta-suifleet/recipes-edge/suifleet/files/suifleet-provision', 'suifleet-edge/packaging/macos/install.sh']) {
  const text = read(installer);
  assert.ok(text.includes(`printf '${prefix}%s\\n' "$address"`), `${installer} does not print "${prefix}"`);
  assert.ok(text.includes('device.address'), `${installer} does not write device.address`);
}
assert.ok(guide.includes(prefix), 'guide does not show the printed address line');
for (const unit of ['suifleet.service', 'suifleet-provision.service', 'suifleet-enrollment.path', 'suifleet-signer.socket']) {
  assert.ok(fs.existsSync(path.join(root, 'suifleet-edge/meta-suifleet/recipes-edge/suifleet/files', unit)), unit);
}
console.log('PASS: in-app commands exist in the daemon; both installers print the documented address line');

// 4. Get started, Devices and Enrollment render the help, with links and commands.
const uses = {
  'components/FirstRun.tsx': 'address', 'app/devices/page.tsx': 'address', 'app/enrollment/page.tsx': 'enroll',
};
for (const [file, variant] of Object.entries(uses)) {
  const source = read(`dapp/src/${file}`);
  assert.match(source, /import \{ DeviceClientHelp \} from "@\/components\/DeviceClientHelp";/, file);
  assert.ok(source.includes(`<DeviceClientHelp variant="${variant}" />`), `${file} does not show the ${variant} help`);
}
const { createElement } = appRequire('react');
const { renderToStaticMarkup } = appRequire('react-dom/server');
const render = (manifest, variant) => {
  if (manifest) process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = manifest; else delete process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST;
  const { DeviceClientHelp } = loader()('components/DeviceClientHelp.tsx');
  return renderToStaticMarkup(createElement(DeviceClientHelp, { variant }));
};
const manifest = read('manifests/fixture.localnet.json');
// One pass, so a decoded "&" never starts another entity.
const ENTITIES = { '&amp;': '&', '&quot;': '"', '&#x27;': "'", '&lt;': '<', '&gt;': '>' };
const decode = (html) => html.replace(/&(?:amp|quot|#x27|lt|gt);/g, (entity) => ENTITIES[entity]);
assert.equal(decode('&amp;lt;&lt;'), '&lt;<');
for (const variant of ['address', 'enroll']) {
  const html = decode(render(manifest, variant));
  const hrefs = [...html.matchAll(/href="([^"]+)"/g)].map((match) => match[1]);
  assert.ok(hrefs.includes(guideUrl()), `${variant}: no guide link`);
  for (const section of ['yocto', 'macos', 'linux', 'debian', 'manifest']) assert.ok(hrefs.includes(guideUrl(section)), `${variant}: no ${section} link`);
  for (const href of hrefs) docPath(href);
  assert.ok(html.includes('Install the device client'));
  assert.ok(html.includes('Download deployment.json'));
  assert.match(html, /rel="noopener noreferrer"/);
  assert.ok(hrefs.includes(guideUrl('signer')) && html.includes('/etc/suifleet/signer.toml'), `${variant}: no signer.toml step`);
  if (variant === 'address') {
    for (const command of [COMMANDS.readAddress, COMMANDS.provisionLog, COMMANDS.deriveAddress, COMMANDS.checkConfig]) assert.ok(html.includes(command), command);
    assert.ok(html.includes(ADDRESS_LINE) && html.includes('64 hexadecimal') && html.includes('not your wallet address'));
  } else {
    assert.ok(html.includes(COMMANDS.enroll) && html.includes('device_cap_id'));
    assert.ok(hrefs.includes(guideUrl('enroll')));
  }
}
assert.ok(!render(undefined, 'address').includes('Download deployment.json'), 'an unconfigured build offers no manifest');
console.log('PASS: Get started, Devices and Enrollment render guide links, address/enroll commands and the manifest download');

async function online() {
  for (const url of new Set(urls.map((url) => url.split('#')[0]))) {
    const response = await fetch(url, { method: 'GET', redirect: 'follow' });
    assert.equal(response.status, 200, `${url}: HTTP ${response.status}`);
    const html = await response.text();
    for (const anchor of Object.values(GUIDE_SECTIONS)) {
      assert.ok(html.includes(`id="${anchor}"`), `${url}: missing served anchor ${anchor}`);
    }
  }
  console.log('PASS: published guide URLs return HTTP 200 with all app-linked anchors');
}
if (process.argv.includes('--online')) online().catch((error) => { console.error(error); process.exit(1); });
