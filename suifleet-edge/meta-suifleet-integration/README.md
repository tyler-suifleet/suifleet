# SuiFleet integration policy

Core-repository companion to meta-suifleet, not part of its standalone Software
layer export. Contains Raspberry Pi access/image policy, disposable test component
and a glibc AArch64 usrmerge layout guard. The guard checks for an incompatible
layout that appeared in a reused local cache; it is not an upstream defect.
Automatic first-boot enrollment comes from the board-independent
`meta-suifleet-enrollment` companion layer. Add that layer alongside the pinned
public recipe on custom systemd images.
The Pi integration layer's OpenSSL/libcap/xz bbappends inherit meta-suifleet's opt-in
`suifleet-core-fixes` class, so standalone consumers can enable the same fixes
with `INHERIT += "suifleet-core-fixes"`. The Pi integration layer depends on
meta-suifleet and meta-raspberrypi. The existing kas profiles enable the
public software layer and both core companion layers. See the [MIT license](../meta-suifleet/COPYING.MIT).
Do not add to arbitrary distros as generic client packaging. No physical acceptance
or production access configuration is established by adding this layer.
