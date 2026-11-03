# Security reporting

Report suspected vulnerabilities privately to the layer maintainer,
Tyler Bush <tyler.jonathan.bush@gmail.com>. Include the layer commit, pinned
source revision, release profile and a minimal reproduction with credentials,
device identity material, personal data and raw scanner reports removed.
Do not post exploit details or secrets in a public issue. Coordinate disclosure
and a fix with the maintainer before publishing details.

This prepared layer is not a production security certification. Only the pinned
Scarthgap/Rust-mixin and Wrynose configurations are declared; no response-time
or long-term security-update guarantee is offered. Track upstream advisories
for the daemon dependencies, Node, codec, OE-Core and selected BSP/distro.
Update source/lock/crate/artifact pins together, review changes and rebuild with
package QA enabled. Never bypass signature, digest or compiler checks.

Package and image builds must contain no runtime device keys, enrollment config,
PINs or journals. Provision identities at runtime and preserve identity/state
on upgrades. Production trust provisioning and physical/mainnet acceptance remain
separate operations requiring their own authorization and evidence.
