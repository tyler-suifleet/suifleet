# SuiFleet integration policy

Core-repository companion to meta-suifleet, not part of its standalone Software
layer export. Contains Raspberry Pi access/image policy, disposable test component
and pinned-profile glibc/OpenSSL/libcap/xz workarounds. Dependencies: meta-suifleet
and meta-raspberrypi (plus their dependencies). The existing kas profiles enable
both layers so Pi package/image behavior is preserved. License: MIT; see ../meta-suifleet/COPYING.MIT.
Do not add to arbitrary distros as generic client packaging. No physical acceptance
or production access configuration is established by adding this layer.
