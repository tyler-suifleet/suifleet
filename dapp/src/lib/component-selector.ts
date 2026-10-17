/** SemVer 2.0 core is bounded to u64 to match Move and Rust. */
export function parseVersion(value: string) {
  const m = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/.exec(value);
  if (!m || m[0] !== value || value.length > 128 || m.slice(1,4).some(n => BigInt(n) > BigInt("18446744073709551615")) || m[4]?.split('.').some(n => /^[0-9]+(?![\s\S])/.test(n) && n.length > 1 && n[0] === '0')) throw new Error('Invalid semantic version');
  return { core: m.slice(1,4).map(BigInt), pre: m[4]?.split('.') };
}
export function compareVersions(a: string, b: string): number {
  const x = parseVersion(a), y = parseVersion(b);
  for (let i=0;i<3;i++) if(x.core[i] !== y.core[i]) return x.core[i] < y.core[i] ? -1 : 1;
  if (!x.pre || !y.pre) return x.pre ? -1 : y.pre ? 1 : a < b ? -1 : a > b ? 1 : 0;
  for(let i=0;i<Math.max(x.pre.length,y.pre.length);i++) {
    const l=x.pre[i], r=y.pre[i]; if(l===r) continue;
    if(l===undefined || r===undefined) return l===undefined ? -1 : 1;
    const ln=/^[0-9]+(?![\s\S])/.test(l), rn=/^[0-9]+(?![\s\S])/.test(r);
    if(ln && rn) return BigInt(l)<BigInt(r)?-1:1;
    if(ln!==rn) return ln?-1:1;
    return l<r?-1:1;
  }
  return a < b ? -1 : a > b ? 1 : 0;
}
export interface PlatformManifest { os: string; arch: string; recipeBlobId: string; recipeSha256: string; artifactSha256: string }
export function validateComponent(name: string, version: string, manifests: PlatformManifest[]) {
  if(!/^[A-Za-z0-9_.-]{1,128}(?![\s\S])/.test(name)) throw new Error('Invalid component name');
  parseVersion(version);
  if(!manifests.length || manifests.length>16) throw new Error('Expected 1–16 manifests');
  const seen = new Set<string>();
  for(const m of manifests) {
    if(!['linux','macos','*'].includes(m.os) || !['x86_64','aarch64','arm','*'].includes(m.arch)) throw new Error('Unsupported manifest platform');
    const key=m.os+'/'+m.arch; if(seen.has(key)) throw new Error('Duplicate platform'); seen.add(key);
    if(!/^[A-Za-z0-9_-]{1,128}(?![\s\S])/.test(m.recipeBlobId) || !/^[a-f0-9]{64}(?![\s\S])/.test(m.recipeSha256) || !/^[a-f0-9]{64}(?![\s\S])/.test(m.artifactSha256)) throw new Error('Invalid manifest blob/digest');
  }
}
export function selectManifest(name:string, version:string, manifests:PlatformManifest[], os:string, arch:string) {
  validateComponent(name,version,manifests);
  const selected=manifests.find(m=>(m.os==='*'||m.os===os)&&(m.arch==='*'||m.arch===arch));
  if(!selected) throw new Error(`Unsupported device platform ${os}/${arch}`);
  return selected;
}
