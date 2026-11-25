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
        setting() {
          printf '%s\n' "$conf" \
            | sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\([^[:space:]]*\).*/\1/p" | tail -n 1
        }
        s=$(setting Storage)
        m=$(setting ProcessSizeMax)
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
