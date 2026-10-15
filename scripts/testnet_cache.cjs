// Only authenticated ciphertext may leave the private runtime directory.
const crypto = require('node:crypto');
const domain = Buffer.from('suifleet/testnet/fixture-cache/v1');
function key(secret) {
  if (!secret) throw Error('missing protected cache secret');
  return crypto.hkdfSync('sha256', Buffer.from(secret), domain, domain, 32);
}
function seal(plaintext, secret) {
  const nonce = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', key(secret), nonce);
  cipher.setAAD(domain);
  const data = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  return Buffer.concat([domain, nonce, cipher.getAuthTag(), data]);
}
function open(data, secret) {
  if (!data.subarray(0, domain.length).equals(domain)) throw Error('invalid cache version');
  const offset = domain.length;
  const decipher = crypto.createDecipheriv('aes-256-gcm', key(secret), data.subarray(offset, offset + 12));
  decipher.setAAD(domain);
  decipher.setAuthTag(data.subarray(offset + 12, offset + 28));
  return Buffer.concat([decipher.update(data.subarray(offset + 28)), decipher.final()]);
}
module.exports = { seal, open };
