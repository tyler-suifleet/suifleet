// Refuse pre-existing files/symlinks at publication time. Callers may remove an
// earlier receipt before starting; an intervening replacement fails closed.
const fs = require('node:fs/promises');
async function writeReceipt(filename, receipt) {
 await fs.writeFile(filename, JSON.stringify(receipt, null, 2) + '\n', {flag:'wx',mode:0o600});
}
module.exports={writeReceipt};
