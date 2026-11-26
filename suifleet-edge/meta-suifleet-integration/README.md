# SuiFleet integration policy

Core-repository companion to meta-suifleet, not part of its standalone Software
layer export. Contains Raspberry Pi access/image policy, disposable test component
and a glibc AArch64 usrmerge layout guard. The guard checks for an incompatible
layout that appeared in a reused local cache; it is not an upstream defect.
Its OpenSSL/libcap/xz bbappends only inherit meta-suifleet's opt-in
`suifleet-core-fixes` class, so standalone consumers can enable the same fixes
with `INHERIT += "suifleet-core-fixes"`. They do not need this layer. Dependencies: meta-suifleet
and meta-raspberrypi (plus their dependencies). The existing kas profiles enable
both layers so Pi package/image behavior is preserved. License: MIT; see ../meta-suifleet/COPYING.MIT.
Do not add to arbitrary distros as generic client packaging. No physical acceptance
or production access configuration is established by adding this layer.
