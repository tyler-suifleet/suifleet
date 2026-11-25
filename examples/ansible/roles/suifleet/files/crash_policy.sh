# Classifies whether a crash of a claim-handling process could store its
# memory (tasks/crash_policy.yml). Run it unescalated in the SSH session that
# will receive the claim: "ulimit -c" is then the core dump size limit that
# session's processes (Ansible's module among them) inherit. Prints one CORE
# line, then exactly one "VERDICT ok" or "VERDICT unsafe <reason>" line.
# Reads only; needs no privilege.
v() { printf 'VERDICT %s\n' "$*"; exit 0; }
# The kernel runs a piped handler in the initial namespaces: it resolves the
# handler's path, and the handler reads its configuration, in the host's
# initial mount namespace and root, not in this session's. A container or
# other isolated root can show a different binary or configuration at the
# same paths, so what this session sees proves nothing about the actual
# handler unless the session shares the host's view. That is verified, never
# assumed, before any piped handler is accepted: this session must be in the
# initial PID and user namespaces (their fixed inode numbers since Linux 3.8),
# so /proc/1 is the host's init and file owners are the host's, and its mount
# table, as seen from its root, must be exactly init's (mount IDs included;
# a separate mount namespace or a chroot shows a different table). This takes
# PID 1, which stays in the initial mount namespace, as the kernel's view.
# Anything else, including an unreadable /proc/1/mountinfo, is refused.
host_view() {
  ns=$(readlink /proc/self/ns/pid 2>/dev/null)/$(readlink /proc/self/ns/user 2>/dev/null)
  [ "$ns" = 'pid:[4026531836]/user:[4026531837]' ] \
    || v unsafe "this session is not in the host's initial PID and user namespaces ($ns), for example in a container, so the piped crash handler $h, which the kernel runs in the host's view, cannot be verified"
  init=$(cat /proc/1/mountinfo 2>/dev/null) && [ -n "$init" ] \
    || v unsafe "init's mount table could not be read, so this session's view of the piped crash handler $h cannot be verified as the host's"
  [ "$init" = "$(cat /proc/self/mountinfo 2>/dev/null)" ] \
    || v unsafe "this session's mounts or root differ from init's, so the piped crash handler $h, which the kernel runs in the host's view, cannot be verified"
}
# The pattern is read from /proc/sys/kernel/core_pattern only once that path
# is shown to be the kernel's sysctl: the mount serving it (the deepest mount
# point containing it, the last such when stacked) must be a procfs whose
# file there is /sys/kernel/core_pattern. A file, another filesystem or another
# procfs entry bind-mounted over it (an isolated view can mask the
# machine-wide pattern this way) is refused, as is an unreadable mount table.
kernel_pattern() {
  t=/proc/sys/kernel/core_pattern
  src=$(LC_ALL=C awk -v t="$t" '
    BEGIN { best = -1 }
    {
      split($0, h, " - "); split(h[2], f, " "); mp = $5
      if (mp == "/") rel = t
      else if (t == mp) rel = ""
      else if (index(t, mp "/") == 1) rel = substr(t, length(mp) + 1)
      else next
      if (length(mp) >= best) { best = length(mp); fs = f[1]; root = ($4 == "/" ? "" : $4); r = rel }
    }
    END { if (best < 0) exit 1; print fs " " root r }' /proc/self/mountinfo 2>/dev/null) \
    || v unsafe "the mount serving $t could not be determined, so kernel.core_pattern cannot be verified as the kernel's"
  [ "$src" = 'proc /sys/kernel/core_pattern' ] \
    || v unsafe "$t is not the kernel's sysctl (it is served by ${src:-nothing}), so the effective kernel.core_pattern cannot be verified"
}
lim=$(ulimit -c) || v unsafe "the core dump size limit could not be read"
kernel_pattern
pat=$(cat /proc/sys/kernel/core_pattern) || v unsafe "kernel.core_pattern could not be read"
printf 'CORE limit=%s pattern=%s\n' "$lim" "$pat"
case $pat in
  '@'*)
    # Linux 6.17 and later connect to a socket collector ("@path" or "@@path")
    # with an unlimited dump size whatever the limit; it may keep the dump.
    v unsafe "core dumps are sent to a socket collector ($pat), which receives them whatever the size limit" ;;
  '|'*)
    # The kernel runs a piped handler whatever the limit; only handlers known
    # to store nothing, or to honor the crashing process's limit, are safe.
    # A handler is identified by its full path, never by its name alone: a
    # site or vendor collector may share a supported handler's name but keep
    # dumps. The path must be one of the distributions' install locations and
    # must resolve (through symlinks such as a merged /usr's /lib) to one,
    # to an existing root-owned executable regular file that only root can
    # modify. Anything else is refused.
    h=${pat#|}
    while :; do case $h in ' '*) h=${h# };; *) break;; esac; done
    h=${h%% *}
    case $h in
      /usr/lib/systemd/systemd-coredump|/lib/systemd/systemd-coredump) kind=systemd-coredump ;;
      /usr/bin/false|/bin/false|/usr/bin/true|/bin/true) kind=discard ;;
      *) v unsafe "the crash handler $h is not a supported handler at a verified location and may store core dumps whatever the size limit" ;;
    esac
    real=$(readlink -f -- "$h" 2>/dev/null) || real=
    case $kind:$real in
      systemd-coredump:/usr/lib/systemd/systemd-coredump|systemd-coredump:/lib/systemd/systemd-coredump) ;;
      discard:/usr/bin/false|discard:/bin/false|discard:/usr/bin/true|discard:/bin/true) ;;
      *) v unsafe "the crash handler $h resolves to ${real:-nothing}, which is not a supported handler at a verified location" ;;
    esac
    [ -f "$real" ] && [ ! -L "$real" ] && [ -x "$real" ] \
      || v unsafe "the crash handler $h is not an installed executable file"
    st=$(stat -c '%u %A' -- "$real" 2>/dev/null) || st=
    case $st in
      '0 -'????'-'??'-'?) ;;
      *) v unsafe "the crash handler $h is not a root-owned file that only root can modify (${st:-unreadable})" ;;
    esac
    # Every piped verdict below that accepts the host first proves this
    # session sees the handler and its configuration as the kernel does.
    case $kind in
      discard) host_view; v ok ;;
      systemd-coredump)
        # systemd-coredump stores nothing for a process whose limit is 0, but
        # it learns that limit only from its sixth argument, which the kernel
        # fills from %c (the kernel itself pipes the dump whatever the limit).
        # Every supported version's pattern starts "%P %u %g %s %t %c"; any
        # other arguments (a literal limit, a reordered or missing %c) mean the
        # handler may not see the actual limit, so the limit counts for
        # nothing and the configuration must disable storage by itself.
        # Otherwise it disables all dump processing and storage only with
        # both Storage=none and ProcessSizeMax=0: with Storage=none alone it
        # may still write the dump to disk temporarily while processing it.
        set -f; set -- ${pat#|}; set +f
        args="$2 $3 $4 $5 $6 $7"
        if [ "$args" = '%P %u %g %s %t %c' ]; then
          [ "$lim" = 0 ] && { host_view; v ok; }
        elif [ "$lim" = 0 ]; then
          lim="0, which the handler does not receive (its arguments start '$args', not '%P %u %g %s %t %c')"
        fi
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
        # So is any syntax whose meaning to systemd this check does not
        # reproduce exactly, rather than guessing it: a byte outside printable
        # ASCII (systemd strips a UTF-8 byte order mark, which would otherwise
        # hide a section header), a section header other than [Name] (systemd
        # stops reading a file at an invalid one) and any other line that is
        # not a comment or a Key=value assignment.
        # cat-config also lists a main file found only in /run, /usr/local/lib
        # or /usr/lib, which some systemd versions' handler never reads (252
        # reads only /etc/systemd/coredump.conf besides the drop-ins), so its
        # settings could look effective without being so. The only main file
        # accepted is /etc/systemd/coredump.conf; any other listed file must be
        # a coredump.conf.d/*.conf drop-in, which every version reads alike.
        eff=$(printf '%s\n' "$conf" | LC_ALL=C awk '
          function classify(l) {
            if (l ~ /[^\t\r -~]/) fail("unsupported")
            sub(/^[ \t]+/, "", l); sub(/[ \t\r]+$/, "", l)
            if (l == "" || l ~ /^[#;]/) return
            if (l ~ /\\$/) cont = 1
            if (l ~ /^\[/) {
              if (l !~ /^\[[A-Za-z0-9_-]+\]$/) fail("unsupported")
              sec = substr(l, 2, length(l) - 2); return
            }
            if (l !~ /^[A-Za-z0-9_]+[ \t]*=/) fail("unsupported")
            if (sec != "Coredump") return
            if (l ~ /^Storage[ \t]*=/) { s = l; sub(/^Storage[ \t]*=[ \t]*/, "", s) }
            if (l ~ /^ProcessSizeMax[ \t]*=/) { m = l; sub(/^ProcessSizeMax[ \t]*=[ \t]*/, "", m) }
          }
          function fail(r) { bad = r; exit }
          left == 0 {
            if ($0 == "" || $0 ~ /^# Main configuration file [^ ]+ not found$/) next
            if ($0 !~ /^# \//) fail("unlisted")
            path = substr($0, 3); n = 0; sec = ""
            if (path != "/etc/systemd/coredump.conf" &&
                path !~ /^\/(etc|run|usr\/local\/lib|usr\/lib|lib)\/systemd\/coredump\.conf\.d\/[^\/]+\.conf$/) fail("main")
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
          unsupported) v unsafe "systemd-coredump's configuration uses syntax this check does not support (a non-ASCII byte, an unusual section header or a line that is not Key=value) and the core dump size limit is $lim" ;;
          main) v unsafe "systemd-coredump's main configuration file is not /etc/systemd/coredump.conf, which not every systemd version reads, and the core dump size limit is $lim" ;;
          continued) v unsafe "systemd-coredump's configuration uses line continuations and the core dump size limit is $lim" ;;
          unreadable) v unsafe "a systemd-coredump configuration file could not be read and the core dump size limit is $lim" ;;
          *) v unsafe "systemd-coredump's configuration listing could not be matched to its files and the core dump size limit is $lim" ;;
        esac
        s=$(printf '%s\n' "$eff" | sed -n 2p)
        m=$(printf '%s\n' "$eff" | sed -n 3p)
        [ "$s" = none ] && [ "$m" = 0 ] && { host_view; v ok; }
        v unsafe "systemd-coredump may write core dumps to disk (Storage=${s:-external}, ProcessSizeMax=${m:-default};" \
          "both none and 0 are required) and the core dump size limit is $lim" ;;
    esac ;;
  *)
    # The kernel itself writes the file, and writes nothing when the limit is 0.
    [ "$lim" = 0 ] && v ok
    v unsafe "core dumps are written to files ($pat) and the core dump size limit is $lim" ;;
esac
