# Classifies the host's active swap for claim staging (tasks/swap_policy.yml).
# Prints "SWAP <ok|unsafe> <device>" per /proc/swaps entry, then exactly one
# VERDICT line: ok, unsafe or unreadable. Reads only; needs no privilege.
v() { printf 'VERDICT %s\n' "$1"; exit 0; }
list=$(cat /proc/swaps) || v unreadable
case $list in Filename*) ;; *) v unreadable;; esac
printf '%s\n' "$list" | sed 1d | {
  bad=0
  while read -r f t rest; do
    [ -n "$f" ] || continue
    b=$(basename "$(readlink -f "$f" 2>/dev/null || printf '%s' "$f")")
    ok=0
    case $t:$b in
      partition:zram[0-9]*)
        w=/sys/block/$b/backing_dev
        if [ ! -e "$w" ]; then
          [ -d "/sys/block/$b" ] && ok=1
        elif [ "$(cat "$w")" = none ]; then
          ok=1
        fi;;
      partition:dm-[0-9]*)
        u=$(cat "/sys/block/$b/dm/uuid" 2>/dev/null) || u=
        case $u in CRYPT-*) ok=1;; esac;;
    esac
    if [ "$ok" = 1 ]; then printf 'SWAP ok %s\n' "$f"; else printf 'SWAP unsafe %s\n' "$f"; bad=1; fi
  done
  [ "$bad" = 0 ] || v unsafe
  v ok
}
