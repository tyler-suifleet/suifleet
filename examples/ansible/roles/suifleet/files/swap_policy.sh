# Classifies the host's active swap for claim staging (tasks/swap_policy.yml).
# Prints "SWAP unsafe <device>" per /proc/swaps entry, then exactly one
# VERDICT line: ok, unsafe, unreadable or unverifiable (preceded by an
# "UNVERIFIABLE <reason>" line). Reads only; needs no privilege.
#
# Any active swap refuses the claim. /proc/swaps names each entry by the path
# it was opened through in the namespace that ran swapon; that text does not
# identify the block device (a node can be named anything, for example a disk
# node called /dev/zram0), and no unprivileged kernel interface binds a swap
# entry to a device number. So the attributes of a device found by that name
# (zram writeback, dm-crypt) cannot be shown to describe the device actually
# swapped to, and swap that looks memory-only or encrypted is refused too.
v() { printf 'VERDICT %s\n' "$1"; exit 0; }
# The swap list is kernel-global, but this session's view of it can be
# substituted: a file bind-mounted over /proc/swaps (or a FUSE view such as
# LXCFS) can list no swap while the kernel swaps to a disk. So it is used only
# once the mount serving it (the deepest mount point containing it, the last
# such when stacked) is shown to be the kernel's procfs with the file at
# /swaps: kernel_view PATH FS REL succeeds only when PATH is served by a mount
# of type FS whose file there is REL. An unreadable mount table fails too.
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
# The entries are parsed by the shell alone, never through a pipe or another
# command: a pipeline's status is its last stage's, so a failed or missing
# stage (for example sed) would hand the loop no input and read as no swap.
# The shell's own builtins cannot fail that way; a "while read" from a
# here-document is avoided too, since some shells write it to a temporary
# file whose failure would also read as empty.
nl='
'
case $list in *"$nl"*) body=${list#*"$nl"} ;; *) body= ;; esac
bad=0
set -f
IFS=$nl
for line in $body; do
  IFS=' 	'
  set -- $line
  [ $# -gt 0 ] || continue
  printf 'SWAP unsafe %s\n' "$1"
  bad=1
done
[ "$bad" = 0 ] || v unsafe
# Cross-check: "ok" only when nothing but whitespace follows the header.
case $body in *[![:space:]]*) v unreadable;; esac
v ok
