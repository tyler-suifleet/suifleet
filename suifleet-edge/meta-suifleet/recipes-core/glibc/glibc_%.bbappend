# Keep the AArch64 usrmerge loader and development linker script consistent.
# Checking before staging also gives this validated layout a distinct sstate
# signature from earlier unvalidated packages. Never repair it with host links
# or suppress package QA: a bad install must stop before it reaches an image.
do_install:append() {
    if [ "${TARGET_ARCH}" = "aarch64" ] && ${@bb.utils.contains('DISTRO_FEATURES', 'usrmerge', 'true', 'false', d)}; then
        loader="${D}${base_libdir}/${ARCH_DYNAMIC_LOADER}"
        if [ ! -f "$loader" ] || [ -L "$loader" ]; then
            bbfatal "AArch64 usrmerge dynamic loader must be a regular target executable in ${base_libdir}"
        fi
        if ! grep -F '${base_libdir}/libc.so.6' ${D}${libdir}/libc.so >/dev/null; then
            bbfatal "AArch64 usrmerge libc linker script does not reference ${base_libdir}/libc.so.6"
        fi
        if ! grep -F '${base_libdir}/${ARCH_DYNAMIC_LOADER}' ${D}${libdir}/libc.so >/dev/null; then
            bbfatal "AArch64 usrmerge libc linker script does not reference its staged dynamic loader"
        fi
    fi
}
