# Classifies the host's active swap for claim staging (tasks/swap_policy.yml).
# Prints "SWAP <ok|unsafe> <device>" per /proc/swaps entry, then exactly one
# VERDICT line: ok, unsafe, unreadable or unverifiable (preceded by an
# "UNVERIFIABLE <reason>" line). Reads only; needs no privilege.
v() { printf 'VERDICT %s\n' "$1"; exit 0; }
# The swap list and the devices' attributes are kernel-global, but this
# session's view of them can be substituted: a file bind-mounted over
# /proc/swaps (or a FUSE view such as LXCFS) can list no swap while the
# kernel swaps to a disk, and a tmpfs or another entry mounted over part of
# /sys can report a disk-backed device as safe. So each path is used only
# once the mount serving it (the deepest mount point containing it, the last
# such when stacked) is shown to be the kernel's filesystem with the path at
# its expected place: kernel_view PATH FS REL succeeds only when PATH is
# served by a mount of type FS whose file there is REL. An unreadable mount
# table fails too.
kernel_view() {
  src=$(LC_ALL=C awk -v t="$1" '
    BEGIN { best = -1 }
    {
      split($0, h, " - "); split(h[2], f, " "); mp = $5
      if (mp == "/") rel = t
      else if (t == mp) rel = ""
      else if (index(t, mp "/") == 1) rel = substr(t, length(mp) + 1)
      else next
      if (length(mp) >= best) { best = length(mp); fs = f[1]; root = ($4 == "/" ? "" : $4); r = rel }
    }
    END { if (best < 0) exit 1; print fs " " root r }' /proc/self/mountinfo 2>/dev/null) || return 1
  [ "$src" = "$2 $3" ]
}
kernel_view /proc/swaps proc /swaps || {
  printf 'UNVERIFIABLE /proc/swaps is not the kernel'"'"'s swap list (it is not served by procfs at /swaps; for example a file or FUSE view is mounted over it), so the active swap cannot be verified\n'
  v unverifiable
}
list=$(cat /proc/swaps) || v unreadable
case $list in Filename*) ;; *) v unreadable;; esac
# A device's attribute counts only when /sys/block is the kernel's, the
# device's entry there resolves to a sysfs directory of the same name under
# /sys/devices, and the attribute file itself is served by sysfs at that place.
sysattr() {
  kernel_view /sys/block sysfs /block || return 1
  d=$(readlink -f "/sys/block/$1" 2>/dev/null) || return 1
  case $d in /sys/devices/*/"$1") ;; *) return 1;; esac
  [ -d "$d" ] && kernel_view "$d" sysfs "${d#/sys}" || return 1
  [ -n "$2" ] || return 0
  kernel_view "$d/$2" sysfs "${d#/sys}/$2"
}
printf '%s\n' "$list" | sed 1d | {
  bad=0
  while read -r f t rest; do
    [ -n "$f" ] || continue
    # The kernel names a swap device by the path it was opened through, which
    # for zram and device-mapper swap is its devtmpfs node /dev/NAME. The name
    # is taken from that text, never by resolving it in this session's /dev.
    b=${f#/dev/}
    ok=0
    case $t:$f in
      partition:/dev/zram[0-9]*)
        case ${b#zram} in ''|*[!0-9]*) b=;; esac
        w=/sys/block/$b/backing_dev
        if [ -z "$b" ] || [ ! -d "/sys/block/$b" ] || ! sysattr "$b"; then
          :
        elif [ ! -e "$w" ]; then
          # Kernels without zram writeback have no backing_dev attribute.
          ok=1
        elif sysattr "$b" backing_dev && [ "$(cat "$w")" = none ]; then
          ok=1
        fi;;
      partition:/dev/dm-[0-9]*)
        case ${b#dm-} in ''|*[!0-9]*) b=;; esac
        if [ -n "$b" ] && sysattr "$b" dm/uuid; then
          u=$(cat "/sys/block/$b/dm/uuid" 2>/dev/null) || u=
          case $u in CRYPT-*) ok=1;; esac
        fi;;
    esac
    if [ "$ok" = 1 ]; then printf 'SWAP ok %s\n' "$f"; else printf 'SWAP unsafe %s\n' "$f"; bad=1; fi
  done
  [ "$bad" = 0 ] || v unsafe
  v ok
}
