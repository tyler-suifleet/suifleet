# install_sw invokes recursive build targets; concurrent targets can relink
# libcrypto while modules/programs link against it, even after do_compile.
# Observed with the pinned Scarthgap 3.5.6 recipe on the ARM64 packaging build.
# Keep compilation parallel and serialize only installation.
# Upstream context: https://github.com/openssl/openssl/issues/27074
PARALLEL_MAKEINST = "-j1"
