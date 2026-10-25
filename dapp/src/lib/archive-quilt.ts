// Bounded reader for the reviewed QuiltV1/RS2/1000-shard producer profile.
// No untrusted length reaches an allocation before range/count validation.
// Authentication is provided by the chain-bound manifest and each item's SHA256.
export function readArchiveQuilt(bytes: Uint8Array): Map<string, Uint8Array> {
  const rows = 334, columns = 667, limit = 4 * 1024 * 1024;
  const symbol = bytes.length / (rows * columns), column = rows * symbol;
  if (!bytes.length || bytes.length > limit || !Number.isInteger(symbol) || symbol < 2 || symbol % 2) throw Error('Unsupported Quilt geometry');
  function read(start: number, offset: number, length: number): Uint8Array {
    if (![start,offset,length].every(Number.isSafeInteger) || start < 0 || offset < 0 || length < 0
      || length > limit || start * column + offset + length > bytes.length) throw Error('Quilt range exceeds parent');
    const out = new Uint8Array(length);
    for (let n = 0; n < length;) {
      const position = offset + n, col = start + Math.floor(position / column);
      const within = position % column, row = Math.floor(within / symbol), inside = within % symbol;
      const count = Math.min(symbol - inside, length - n);
      const physical = row * columns * symbol + col * symbol + inside;
      out.set(bytes.subarray(physical,physical+count),n); n += count;
    }
    return out;
  }
  const header = read(0,0,5), view = new DataView(header.buffer);
  const size = view.getUint32(1,true);
  if (header[0] !== 1 || size < 1 || size > 4096 || size + 5 > bytes.length) throw Error('Quilt index bounds');
  const index = read(0,5,size); let offset = 0;
  const byte = () => { if (offset >= index.length) throw Error('Truncated Quilt index'); return index[offset++]; };
  const uleb = () => {
    let value = 0;
    for (let n = 0; n < 4; n++) { const b = byte(); value += (b & 127) * 2 ** (7*n); if (b < 128) return value; }
    throw Error('Quilt integer bounds');
  };
  const text = () => {
    const length = uleb();
    if (length < 1 || length > 64 || offset + length > index.length) throw Error('Quilt identifier bounds');
    const value = new TextDecoder('utf-8',{fatal:true}).decode(index.subarray(offset,offset+length)); offset += length;
    if (!/^[a-zA-Z0-9_-]{1,64}$/.test(value)) throw Error('Invalid Quilt identifier');
    return value;
  };
  const count = uleb();
  if (count < 2 || count > 17) throw Error('Quilt item count bounds');
  let start = Math.ceil((size + 5) / column);
  const result = new Map<string,Uint8Array>();
  for (let n = 0; n < count; n++) {
    const end = byte() + 256 * byte(), identifier = text(), tags = uleb();
    if (tags !== 0 || end <= start || end > columns || result.has(identifier)) throw Error('Quilt patch/index bounds');
    const part = read(start,0,8), h = new DataView(part.buffer);
    const length = h.getUint32(1,true), nameSize = h.getUint16(6,true);
    // This producer emits no tags; their presence is an unsupported format,
    // rather than an invitation to parse attacker-controlled nested metadata.
    if (part[0] !== 1 || part[5] !== 0 || nameSize < 2 || nameSize > 65
      || length < 2 + nameSize || length + 6 > (end-start)*column) throw Error('Quilt patch header bounds');
    const name = read(start,8,nameSize);
    if (name[0] !== nameSize-1 || new TextDecoder('utf-8',{fatal:true}).decode(name.subarray(1)) !== identifier) throw Error('Quilt patch identifier mismatch');
    result.set(identifier,read(start,8+nameSize,length-2-nameSize)); start = end;
  }
  if (offset !== index.length || !result.has('manifest')) throw Error('Invalid Quilt index framing');
  return result;
}
