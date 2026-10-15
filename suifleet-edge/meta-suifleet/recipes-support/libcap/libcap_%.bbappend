# Wrynose libcap 2.77's install target can relink capsh. The upstream
# recipe passes these tools only to do_compile, so that relink otherwise
# falls back to the host gcc while retaining target CFLAGS/LDFLAGS.
# Keep this workaround out of Scarthgap's older libcap metadata.
python __anonymous() {
    if 'wrynose' in (d.getVar('LAYERSERIES_CORENAMES') or '').split():
        d.appendVar('EXTRA_OEMAKE', ' CC="${CC}" AR="${AR}" RANLIB="${RANLIB}" OBJCOPY="${OBJCOPY}" BUILD_CC="${BUILD_CC}" BUILD_LDFLAGS="${BUILD_LDFLAGS}"')
}
