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
        # each file and the last assignment wins. cat-config prints "# /path"
        # before each file, in precedence order, but a comment inside a file
        # can look the same, so the boundaries are not taken from the text:
        # each listed file is read directly and exactly its lines are consumed
        # (and must match), so its comments can never start another file.
        # Anything else in the listing, a file that cannot be read and line
        # continuations (which could hide or forge an assignment) are refused.
        eff=$(printf '%s\n' "$conf" | awk '
          function classify(l) {
            sub(/^[ \t]+/, "", l); sub(/[ \t\r]+$/, "", l)
            if (l == "" || l ~ /^[#;]/) return
            if (l ~ /\\$/) cont = 1
            if (l ~ /^\[.*\]$/) { sec = substr(l, 2, length(l) - 2); return }
            if (sec != "Coredump") return
            if (l ~ /^Storage[ \t]*=/) { s = l; sub(/^Storage[ \t]*=[ \t]*/, "", s) }
            if (l ~ /^ProcessSizeMax[ \t]*=/) { m = l; sub(/^ProcessSizeMax[ \t]*=[ \t]*/, "", m) }
          }
          function fail(r) { bad = r; exit }
          left == 0 {
            if ($0 == "" || $0 ~ /^# Main configuration file [^ ]+ not found$/) next
            if ($0 !~ /^# \//) fail("unlisted")
            path = substr($0, 3); n = 0; sec = ""
            while ((r = (getline l < path)) > 0) lines[++n] = l
            close(path)
            if (r < 0) fail("unreadable")
            left = n; i = 0; next
          }
          { if ($0 != lines[++i]) fail("changed"); classify($0); left-- }
          END {
            # The shell drops the trailing newlines of the whole listing,
            # which removes the last file'"'"'s trailing blank lines.
            while (bad == "" && left && lines[i + 1] == "") { i++; left-- }
            if (bad == "" && left) bad = "changed"
            if (bad == "" && cont) bad = "continued"
            printf "%s\n%s\n%s\n", (bad == "" ? "-" : bad), s, m
          }') \
          || v unsafe "systemd-coredump's configuration could not be parsed and the core dump size limit is $lim"
        case $(printf '%s\n' "$eff" | sed -n 1p) in
          -) ;;
          continued) v unsafe "systemd-coredump's configuration uses line continuations and the core dump size limit is $lim" ;;
          unreadable) v unsafe "a systemd-coredump configuration file could not be read and the core dump size limit is $lim" ;;
          *) v unsafe "systemd-coredump's configuration listing could not be matched to its files and the core dump size limit is $lim" ;;
        esac
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
