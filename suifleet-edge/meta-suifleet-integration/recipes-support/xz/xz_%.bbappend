# Wrynose's generated xzmore selects bash, but the core recipe declares
# bash only for xz-ptest. Use the usrmerge interpreter path that bash's
# file-provider metadata advertises, and declare the runtime dependency.
# Register the change only on Wrynose, preserving Scarthgap task inputs.
suifleet_xz_interpreter() {
    sed -i '1s|^#!/bin/bash$|#!${bindir}/bash|' ${D}${bindir}/xzmore
}

python __anonymous() {
    if 'wrynose' in (d.getVar('LAYERSERIES_CORENAMES') or '').split() and d.getVar('CLASSOVERRIDE') == 'class-target':
        d.appendVar('RDEPENDS:' + d.getVar('PN'), ' bash')
        d.appendVar('do_install', '\n    suifleet_xz_interpreter\n')
}
