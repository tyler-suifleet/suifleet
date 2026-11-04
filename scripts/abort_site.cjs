// Share the source-pinned selector with Python acceptance runners.
const {execFileSync} = require('node:child_process');
const path = require('node:path');
exports.abortSite = (source, module, fn, code) => execFileSync('python3',
  [path.join(__dirname, 'abort_site.py'), source, module, fn, String(code)],
  {encoding: 'utf8'}).trim();
