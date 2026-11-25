# Classifies whether a crash of a claim-handling process could store its
# memory (tasks/crash_policy.yml). Run it unescalated in the SSH session that
# will receive the claim: "ulimit -c" is then the core dump size limit that
# session's processes (Ansible's module among them) inherit. Prints one CORE
# line, then exactly one "VERDICT ok" or "VERDICT unsafe <reason>" line.
# Reads only; needs no privilege.
v() { printf 'VERDICT %s\n' "$*"; exit 0; }
lim=$(ulimit -c) || v unsafe "the core dump size limit could not be read"
pat=$(cat /proc/sys/kernel/core_pattern) || v unsafe "kernel.core_pattern could not be read"
printf 'CORE limit=%s pattern=%s\n' "$lim" "$pat"
case $pat in
  '|'*)
    # The kernel runs a piped handler whatever the limit; only handlers known
    # to store nothing, or to honor the crashing process's limit, are safe.
    h=${pat#|}
    while :; do case $h in ' '*) h=${h# };; *) break;; esac; done
    h=${h%% *}
    h=${h##*/}
    case $h in
      false|true) v ok ;;
      systemd-coredump)
        # systemd-coredump stores nothing for a process whose limit is 0.
        # Otherwise it disables all dump processing and storage only with
        # both Storage=none and ProcessSizeMax=0: with Storage=none alone it
        # may still write the dump to disk temporarily while processing it.
        [ "$lim" = 0 ] && v ok
        conf=$(systemd-analyze cat-config systemd/coredump.conf 2>/dev/null) \
          || v unsafe "systemd-coredump's configuration could not be read and the core dump size limit is $lim"
        # Resolve the effective [Coredump] values as systemd does: only
        # assignments inside a [Coredump] section count, the section resets at
        # each file (cat-config prints "# /path" before each file, in
        # precedence order) and the last assignment wins. Line continuations
        # could hide or forge an assignment, so they are refused outright.
        eff=$(printf '%s\n' "$conf" | awk '
          /^# \// { sec = ""; next }
          { sub(/^[ \t]+/, ""); sub(/[ \t\r]+$/, "") }
          $0 == "" || /^[#;]/ { next }
          /\\$/ { cont = 1 }
          /^\[.*\]$/ { sec = substr($0, 2, length($0) - 2); next }
          sec != "Coredump" { next }
          /^Storage[ \t]*=/ { s = $0; sub(/^Storage[ \t]*=[ \t]*/, "", s) }
          /^ProcessSizeMax[ \t]*=/ { m = $0; sub(/^ProcessSizeMax[ \t]*=[ \t]*/, "", m) }
          END { printf "%s\n%s\n%s\n", (cont ? "continued" : "-"), s, m }') \
          || v unsafe "systemd-coredump's configuration could not be parsed and the core dump size limit is $lim"
        [ "$(printf '%s\n' "$eff" | sed -n 1p)" = - ] \
          || v unsafe "systemd-coredump's configuration uses line continuations and the core dump size limit is $lim"
        s=$(printf '%s\n' "$eff" | sed -n 2p)
        m=$(printf '%s\n' "$eff" | sed -n 3p)
        [ "$s" = none ] && [ "$m" = 0 ] && v ok
        v unsafe "systemd-coredump may write core dumps to disk (Storage=${s:-external}, ProcessSizeMax=${m:-default};" \
          "both none and 0 are required) and the core dump size limit is $lim" ;;
      *) v unsafe "the crash handler $h may store core dumps whatever the size limit" ;;
    esac ;;
  *)
    # The kernel itself writes the file, and writes nothing when the limit is 0.
    [ "$lim" = 0 ] && v ok
    v unsafe "core dumps are written to files ($pat) and the core dump size limit is $lim" ;;
esac
