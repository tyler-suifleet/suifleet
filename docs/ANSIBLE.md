# Ansible provisioning and fleet rollout (SF-304)

[`examples/ansible`](../examples/ansible) installs, enrolls, configures,
upgrades and restarts the SuiFleet device client on existing Linux hosts. It
uses the same files and order as the [install guide](INSTALL_DEVICE_CLIENT.md):

1. Install the package or binary. This starts nothing.
2. Each device generates its own key, offline, with `suifleet-provision.service`.
3. The device enrolls itself with a single-use claim from your credential batch.
4. Install `deployment.json`, then `signer.toml`, then `config.toml`.
5. Start the signing socket and `suifleet-enrollment.path`, which starts the daemon.
6. Check daemon health before the next batch.

The examples are reviewed starting points, not a hosted service. Test them on
your own disposable hosts before production. They never reboot a host. Reboot,
OS installation and firmware rollback stay with your platform tools
([platform installers](PLATFORM_INSTALLERS.md)).

## Supported profiles

| Profile | Hosts | Artifact | Tested by `make test-ansible` |
|---|---|---|---|
| `deb` | Debian 12, Ubuntu 24.04; amd64 or arm64; systemd; Linux 6.5 or later | Local-file `.deb` ([Debian packaging](DEBIAN_PACKAGING.md)) | Debian 12 and Ubuntu 24.04 amd64 containers |
| `systemd-binary` | Other systemd distributions; amd64 or arm64; Linux 6.5 or later | Release binary plus the reviewed `meta-suifleet` unit files | Debian 12 amd64 container |
| `auto` (default) | `deb` on the tested distributions, otherwise `systemd-binary` when a binary is configured | | |

The role stops before changing anything on these hosts:

- **No systemd.** The signing service needs systemd socket activation and
  caller-unit pinning. No OpenRC, SysV or runit files exist. A host without
  systemd is refused.
- **Linux older than 6.5.** The signer pins its caller with `SO_PEERPIDFD`
  ([key isolation](KEY_ISOLATION.md)).
- **Other architectures.** Only x86_64 and aarch64 have packaged clients.
- **`deb` on an untested distribution.** This needs
  `suifleet_allow_untested_distribution: true`. Releases match exactly:
  Debian `12` covers its 12.x point releases, while Ubuntu `24.04` excludes
  24.10 and other non-LTS releases (`suifleet_deb_distributions`).

Yocto devices get the client from the image recipe
([Yocto guide](CUSTOMER_YOCTO.md)), not from Ansible. macOS has its own
installer ([macOS guide](CUSTOMER_MACOS.md)).

**Self-update.** The packaged daemon is replaced by the package manager, so the
role does not configure [daemon self-update](DAEMON_UPDATE.md). Choose one
update owner per host: Ansible (this role) or SuiFleet self-update.

## Artifacts

No public APT repository or published download exists yet (SF-042/#62). Obtain
a signed release bundle from your fleet operator and verify it on the
controller, using a verifier and trust roots you already trust:

```sh
suifleet --verify-release /srv/suifleet/release-0.2.3 /etc/suifleet/release-trust.toml x86_64-unknown-linux-gnu
```

Copy each artifact's SHA-256 from that verified `release.json` into the
inventory (`suifleet_deb_sha256`). The role checks the SHA-256 on the
controller, then stages the artifact under `/var/cache/suifleet` and checks
the staged copy on each host before installing. A binary replaces
`/usr/bin/suifleet` only after its staged copy matches. For
`systemd-binary`, extract the binary from the verified tarball and pin its own
SHA-256 (`suifleet_binary_sha256`). Point `suifleet_units_src` at the reviewed
`suifleet-edge/meta-suifleet/recipes-edge/suifleet/files` directory of the same
release.

## Inputs

Copy [`inventory.example.yml`](../examples/ansible/inventory.example.yml) outside
source control. The inventory holds no secrets and no device addresses.

| Variable | Scope | Meaning |
|---|---|---|
| `suifleet_serial` | host | The host's serial: its folder in the credential batch. |
| `suifleet_manifest_src` | group | The fleet's `deployment.json` on the controller. |
| `suifleet_enrollment_root` | group | The extracted credential batch on the controller. |
| `suifleet_deb_src`, `suifleet_deb_sha256` | group | The verified package and its SHA-256 (`deb`). |
| `suifleet_binary_src`, `suifleet_binary_sha256`, `suifleet_units_src` | group | The verified binary, its SHA-256 and the unit files (`systemd-binary`). |
| `suifleet_profile` | any | `auto`, `deb` or `systemd-binary`. |
| `suifleet_apply_command` | group | Your reviewed platform installer. The default `/usr/bin/false` installs nothing. |
| `suifleet_apply_timeout_seconds`, `suifleet_apply_allow_recipe_commands`, `suifleet_apply_mode`, `suifleet_poll_seconds`, `suifleet_replay_history` | group | The matching `config.toml` settings. |
| `suifleet_config_extra`, `suifleet_signer_extra` | group | Extra reviewed TOML for `config.toml` and `signer.toml`. Each block starts with its own `[table]`. Never put secrets here. |
| `suifleet_rollout_batches` | play | Rollout batches. Default `[1, "25%", "100%"]`: one canary host, then a quarter, then the rest. |
| `suifleet_max_fail_percentage` | play | Default `0`: any failed host stops later batches. |
| `suifleet_hosts` | play | Inventory group. Default `suifleet_devices`. |

All defaults are in [`roles/suifleet/defaults/main.yml`](../examples/ansible/roles/suifleet/defaults/main.yml).

**The only secret input is the credential batch.** Create it with the dApp's
Enrollment page (*Download credentials*), or offline:

```sh
scripts/claims.py generate --roster devices.csv /secure/batch-07 --template 'line-{n:4}' \
  --policy POLICY --manifest deployment.json
```

Each `enrollment/SERIAL/` folder holds `claim.key`, `enroll.toml` and
`deployment.json` ([enrollment](ENROLLMENT.md)). Import the public
`roster.json` on the Enrollment page, then run its pending transactions to
register and fund the claims. Keep the batch on encrypted controller storage,
or encrypt each claim with Ansible Vault:

```sh
ansible-vault encrypt /secure/batch-07/enrollment/*/claim.key
```

Then pass `--ask-vault-pass` or `--vault-password-file` to `ansible-playbook`.
The role reads a host's claim only while that host is not yet enrolled.
Delete the batch once every host is enrolled.

## Run

Hosts need SSH, sudo and Python 3; `deb` hosts also need `python3-apt` (present on
standard Debian and Ubuntu installs) for the `apt` module. Enrolling hosts also
need a tmpfs the SSH account can write (`/dev/shm` by default, see
`suifleet_claim_transfer_dir`), no swap that can write to a disk and no crash
handler that can store a core dump while they enroll
([claim storage](#claim-storage)). Use a controller with
ansible-core 2.16 or later. The examples use only
`ansible.builtin` modules. The tested controller is pinned in
[`requirements-controller.txt`](../examples/ansible/requirements-controller.txt):

```sh
cd examples/ansible
python3 -m venv ~/.venvs/suifleet-ansible
~/.venvs/suifleet-ansible/bin/pip install --require-hashes -r requirements-controller.txt
ansible-playbook -i ~/fleet/inventory.yml site.yml --check --diff   # preview
ansible-playbook -i ~/fleet/inventory.yml site.yml                  # install, enroll, start
```

After enrollment, run the Enrollment page's pending transactions again. They
bind each enrolled claim to its serial, alias and group. Then fund device gas
with *Fund enrolled devices*. Ansible holds no wallet key and sends no
transactions other than each device's own claim-sponsored enrollment.

**Upgrade.** Change the artifact path and SHA-256, then run `site.yml` again.
The package stops the units, and the role starts them again batch by batch.
Keys, address, enrollment record, configuration and journal are preserved.

**Restart.** `ansible-playbook -i INVENTORY restart.yml` restarts the daemon
one host at a time. It checks health and that the address did not change.

**Idempotence.** Rerunning `site.yml` on converged hosts reports no changes.
Check mode reports pending installs, upgrades and enrollments without
performing them; an upgrade preview leaves the installed bytes, package and
running services untouched.

**Interrupted runs.** Restarts are not tied only to the current run's changes.
After the units pass their health checks, the role records a SHA-256 digest
of each unit's inputs in `/var/lib/suifleet-ansible/applied.json` (root-owned,
public digests only). The inputs are the unit's own files plus
`suifleet_signer_inputs` or `suifleet_daemon_inputs`; the signing socket and
`suifleet-enrollment.path` are covered by their unit files. On the next run,
an active unit whose inputs differ from that record (or that has no record),
that waits for a daemon-reload, or whose process runs a replaced executable is
restarted. No timestamps are compared, so a change made just after a restart
is still detected. A rollout that stopped after its files landed but before
the restarts therefore converges on the next run.

A changed signing socket unit is applied in dependency order: the signing
service stops, the socket is recreated with the new settings (for example
its mode or group), then the signing service and the daemon restart. A
changed `suifleet-enrollment.path` is restarted. The health check fails while
any of these units still runs replaced inputs. A host that already runs
SuiFleet but has no record (for example one installed by hand) has its units
restarted once.

## Security properties

- **Device keys never leave the device.** Each host generates its own key; the
  role checks only the key's owner and mode. It never reads, copies or fetches
  the key, and never puts a key in an image, package or Git.
- **Claims are single-use and never reach persistent storage.** The
  controller reads the claim into memory (Ansible Vault files are decrypted in
  memory, not into its local temporary directory). It reaches the host as the
  `no_log` standard input of a command, inside the pipelined module stream.
  That command runs **without privilege escalation**, as the SSH account:
  sudo can record a command's standard input (sudoers `log_input`), and with
  pipelining that input is the whole module, claim included. An inventory
  that sets `ansible_become: true` (which overrides the `become: false` task
  keyword) is handled: the transfer tasks set `ansible_become: false` as a
  task variable. Extra vars outrank task variables, so the controller
  first refuses if the effective `ansible_become` is still true; never pass
  `-e ansible_become=true`. Before reading the claim, the role then requires
  positive evidence on the host: the module's complete process chain, from
  its interpreter up to the SSH session (`sshd`), must be readable, run
  entirely as the module's own user and carry no Ansible become wrapper.
  An unreadable chain (for example `/proc` mounted with `hidepid`), another
  user ID in it (a `sudo`/`su`/`doas`-wrapped `ansible_python_interpreter`)
  or a connection that is not SSH refuses the claim. The SSH account
  writes the claim into a private directory on `suifleet_claim_transfer_dir`
  (default `/dev/shm`, which must be a tmpfs). A root task, whose module
  carries only paths, then has the current key holder (the daemon user, not
  root) copy it into a private staging directory on a memory-backed `/run`,
  and the transfer directory is erased. Ansible's file copy,
  which goes through the remote temporary directory, is never used for the
  claim. Enrollment refuses to send a claim unless modules are pipelined, so
  keep `pipelining = True` and leave `ANSIBLE_KEEP_REMOTE_FILES` unset. The
  staging directory is erased after enrollment, when it fails, at the start
  of the next attempt and on every run of an enrolled host (a run interrupted
  after the record was written but before its cleanup). Every run also erases
  the SSH account's transfer directories left by a killed run. An interrupted
  run leaves the claim only on those tmpfs mounts until the next run or a reboot. Revoke a claim that was exposed before use.
  tmpfs alone is not enough, because its pages and the memory of the
  processes holding the claim can be swapped out or saved in a crash dump; see
  [claim storage](#claim-storage).
- **One enrollment per device.** `/etc/suifleet/enrollment.json` records the
  public IDs, and a recorded host never enrolls again. A host that has
  `config.toml` but no record is refused rather than given a new claim.
- **Records are checked against the key.** A record whose address differs from
  the device's own key stops the play.
- **No secrets in command arguments or logs.** Commands receive file paths
  only; the claim arrives on standard input. `ansible.cfg` sets no log file.
  The daemon journal stays on the host; only health exit codes are reported.
- **Least privilege.** Connect as an unprivileged account with sudo; never SSH
  as root. Package installation, service control and file ownership need root.
  The daemon and the signing service run as separate unprivileged users
  ([key isolation](KEY_ISOLATION.md)). Firmware helpers are not installed.

### Claim storage

The claim must never reach persistent storage on the host, neither as a file
nor as swapped-out memory. Linux can swap tmpfs pages, and the processes
that handle the claim (the transfer command, `runuser` and `suifleet
--enroll`) hold it in memory. Before the claim is read, the role reads
`/proc/swaps` and refuses the host unless every active swap
device is one of:

- **zram without a writeback device** (`/sys/block/zramN/backing_dev` is
  `none` or absent): compressed RAM, never written to a disk;
- **a dm-crypt device** (device-mapper UUID `CRYPT-…`, for example
  `cryptsetup` plain swap with a random key or LUKS): the kernel writes only
  ciphertext.

A swap file, a plain disk partition, zram that writes back to a disk, swap on
LVM or another device-mapper target (even on top of dm-crypt) and anything
unrecognized refuse the claim, and the message names the devices.

The session's view of the swap list and device attributes can be
substituted (a file or an LXCFS-style FUSE view bind-mounted over
`/proc/swaps` can list no swap while the kernel swaps to a disk; a tmpfs or
another entry mounted over part of `/sys` can report a disk-backed device as
safe), so the role uses them only once `/proc/self/mountinfo` shows they come
from the kernel. `/proc/swaps` must be served by procfs at `/swaps`;
otherwise, or when the mount table cannot be read, the host is refused as
unverifiable before the list is read. A device counts as zram or dm-crypt
only when the kernel's swap list names it `/dev/zramN` or `/dev/dm-N` (the
name is never resolved through the session's `/dev`), `/sys/block` is served
by sysfs at `/block`, the device's entry resolves to a sysfs directory of the
same name under `/sys/devices`, and the attribute read (`backing_dev`,
`dm/uuid`) is served by sysfs at that place; otherwise the device is unsafe.
Enroll from a session that sees the kernel's `/proc` and `/sys`, not from
inside a container that masks them. Hibernation
writes memory to swap, so it is covered by the same rule. For a host with
ordinary disk swap, run `swapoff -a` before enrolling it and `swapon -a`
afterwards (recorded hosts never handle a claim again), or move it to zram
or encrypted swap.

A crash dump is also a copy of a process's memory. Before the claim is read,
the role reads `kernel.core_pattern` and, without privilege escalation, the
core dump size limit (`ulimit -c`) that the SSH session receiving the claim
inherits. It first checks that `/proc/sys/kernel/core_pattern` is the
kernel's sysctl: the mount serving it must be a procfs whose entry there is
`/sys/kernel/core_pattern`. A file, another filesystem or another procfs entry
bind-mounted over it (an isolated view can mask the machine-wide pattern this
way) is refused, and so is a mount table that cannot be read. It then refuses
the host unless a crash could store nothing:

- **`kernel.core_pattern` names a file** (for example Debian's default
  `core`) and the session's limit is 0: the kernel writes nothing;
- **it pipes to `systemd-coredump`** and either the session's limit is 0
  and the pattern's arguments start `%P %u %g %s %t %c` (systemd-coredump
  honors the crashing process's limit, which it reads only from that sixth
  `%c` argument; a literal value there, as on some hosts, means a limit of 0
  counts for nothing), or `coredump.conf`
  sets both `Storage=none` and `ProcessSizeMax=0`. With `Storage=none` alone,
  systemd-coredump may still write the dump to disk temporarily while
  processing it. As in systemd, only assignments inside a `[Coredump]`
  section count (each file, including each drop-in, starts outside any
  section) and the last one wins. File boundaries come from reading each file
  that `systemd-analyze cat-config` lists, so a comment that looks like its
  `# /path` header does not end a section. A configuration that cannot be
  read, whose listing does not match its files, or that uses line
  continuations, is refused. So is syntax whose meaning to systemd the check
  does not reproduce exactly: any byte outside printable ASCII (including a
  UTF-8 byte order mark, which systemd strips), a section header other than
  `[Name]` (systemd stops reading the file at an invalid one) and any other
  line that is not a comment or a `Key=value` assignment. The only main file
  accepted is `/etc/systemd/coredump.conf`; every other listed file must be a
  `coredump.conf.d/*.conf` drop-in. When `/etc/systemd/coredump.conf` is
  absent, `cat-config` lists a main file found in `/run`, `/usr/local/lib` or
  `/usr/lib` instead, but systemd 252's handler (Debian 12) reads only the
  `/etc` file besides the drop-ins, so such a file's settings may not apply
  and the host is refused;
- **it pipes to `false` or `true`**: nothing is kept.

A socket collector (`@path` or `@@path`, Linux 6.17 and later) is refused: the
kernel sends it the dump with an unlimited size whatever the limit.

A piped handler is identified by its full path, never by its name: it must be
`/usr/lib/systemd/systemd-coredump` or `/lib/systemd/systemd-coredump`, or
`false`/`true` in `/usr/bin` or `/bin`, must resolve through any symlinks
(such as a merged `/usr`'s `/lib`) to one of those paths, and must be an
existing root-owned executable regular file that only root can modify. A
collector elsewhere that shares a supported handler's name (for example
`/usr/local/bin/systemd-coredump`), or a `true` that resolves to a multi-call
binary, is refused. Any other piped handler is refused, because the kernel
runs it whatever the limit and it may keep the dump: Apport (Ubuntu's
default), ABRT, the WSL crash handler or a vendor handler. The message gives the reason.

The kernel runs a piped handler in the host's initial namespaces: it resolves
the handler's path, and the handler reads its configuration, in the host's
initial mount namespace and root, not the SSH session's. A container or
other isolated root can show a different binary or `coredump.conf` at the same
paths. So a piped handler is accepted only when the session provably shares
the host's view: it must be in the initial PID and user namespaces (fixed
namespace inode numbers since Linux 3.8, so `/proc/1` is the host's init) and
its `/proc/self/mountinfo` must equal init's `/proc/1/mountinfo` (mount IDs
included; a separate mount namespace or a chroot differs). PID 1 is taken as
the kernel's view. Otherwise every piped handler, even systemd-coredump with
`Storage=none` and `ProcessSizeMax=0` or a session limit of 0, is refused; on
such a host use a file `kernel.core_pattern` with a session limit of 0, which
the kernel applies in the crashing process's own namespace. To enroll
such a host, stop the handler for the enrollment (for example
`systemctl stop apport`, which restores a file pattern), add
a drop-in in `/etc/systemd/coredump.conf.d/` whose `[Coredump]` section sets
`Storage=none` and `ProcessSizeMax=0` on systemd-coredump hosts,
or set the deploy account's `core` limit to 0 in `/etc/security/limits.d/`
(`pam_limits` sets SSH sessions' limits, starting from PID 1's, so
`LimitCORE=0` on the SSH service alone may not reach them). The root-run staging and enrollment commands
set their own limit to 0 after `sudo` and `runuser` apply their PAM limits.

These checks cover the hosts. The controller also holds the claim in memory
(`ansible-playbook` reads and decrypts it); run it on a machine whose swap
is encrypted or off and whose crash dumps are disabled, for example with
`ulimit -c 0` in the shell that starts `ansible-playbook` and no piped crash
handler that ignores that limit (macOS writes `/cores` only when the limit
allows it). The role does not check the controller.

## Recovery

- **A failed batch stops the rollout.** Fix the cause, then run `site.yml`
  again. Completed hosts report no changes; hosts whose services still run
  replaced inputs are restarted.
- **Interrupted enrollment.** If enrollment ran on chain but the record was
  not written, `/etc/suifleet/.enrollment.json.partial` may hold its public
  output. The Enrollment page also shows the device as *Enrolled*, with its IDs
  under *Technical details*. Restore the record as root, then run the play:

  ```sh
  install -m 0640 -o root -g suifleet /dev/stdin /etc/suifleet/enrollment.json <<'EOF'
  {"device_address":"0x…","device_cap_id":"0x…","device_object_id":"0x…","digest":"…","owner_cap_id":"0x…"}
  EOF
  ```

  Never issue an enrolled device a new claim, and never replace its key.
- **Swap refusal.** See [claim storage](#claim-storage): enroll with swap
  off, on zram without writeback or on dm-crypt swap. Nothing was sent.
- **Crash dump refusal.** See [claim storage](#claim-storage): disable core
  dumps for the enrollment (limit 0 with a file pattern or systemd-coredump,
  or `Storage=none` with `ProcessSizeMax=0` under `[Coredump]`). Nothing was
  sent.
- **A lost or exposed claim before enrollment.** Revoke it on the Enrollment
  page (*Lost a credential file?*).
- **Removing a host** from the fleet is a separate, deliberate decision. The
  role never purges identity or journals.

## Validation

`make test-ansible` runs [`scripts/test_ansible.py`](../scripts/test_ansible.py)
on a Docker host. It needs the debug daemon and `chain_facts` example, a warm
Move dependency cache volume (`SUIFLEET_MOVE_CACHE`; the dev image comes from `SUIFLEET_DEV_IMAGE`), and network access once
to build the test images. Everything else runs in one loopback-only network
namespace:

- **Chain.** A disposable localnet with the current package. It creates an
  enrollment policy and a `claims.py` batch, registered with the PTB printed by
  `claims.py ptb`.
- **Hosts.** Real systemd containers reached over SSH with host-key checking,
  as an unprivileged sudo user: Debian 12 and Ubuntu 24.04 (`deb`) and Debian
  12 (`systemd-binary`). A host without systemd checks the refusal.
- **Controller.** The pinned ansible-core. One claim is Vault-encrypted.
- **sudo input logging.** The hosts' sudoers set `Defaults log_input,
  !compress_io`, so sudo writes every become command's standard input to
  `/var/log/sudo-io` in plain form.

The harness checks these phases:

1. Profile selection with injected facts: Debian 12.x and Ubuntu 24.04 select
   `deb`; Ubuntu 24.10 and Debian 13 are refused for `deb` unless explicitly
   allowed.
2. Refusal of the host without systemd.
3. Check mode on fresh hosts changes nothing. A control play sends a
   disposable marker as the `no_log` standard input of a become command (how
   the claim used to travel), and the marker must appear in every host's sudo
   I/O logs. The inventory sets `ansible_become: true` as a connection
   variable for every later phase. Three claim transfers must be refused
   before any staging or transfer directory exists: one run with
   `-e ansible_become=true` (refused on the controller), one whose
   `ansible_python_interpreter` is wrapped in `sudo -u nobody` (refused by the
   host probe because the process chain changes user), and the same with
   `/proc` remounted `hidepid=2` (refused because the chain is not fully
   readable). Swap is checked next. The hosts share the test machine's
   kernel; the role's own classifier is run on them first, and when it
   reports that machine's swap unsafe (for example WSL's disk swap), a claim
   transfer with it unchanged must be refused, naming the device. Accepted
   real swap (zram without writeback, dm-crypt) skips that case. Then
   `/proc/swaps` (and `/sys/block`) are replaced inside each host's mount
   namespace; nothing is swapped on or off. The committed role must refuse a
   transfer, as unverifiable, with a header-only file bind-mounted over
   `/proc/swaps`. A test-only copy whose swap classifier trusts only that
   `/proc/swaps` fixture (its device attribute checks unchanged) must refuse a
   transfer, naming both devices, with zram `backing_dev` `none` and a
   dm-crypt UUID on a tmpfs over `/sys/block`. The remaining swap fixtures run
   through a test-only copy whose swap classifier's `kernel_view` and
   `sysattr` checks always pass (synthetic classifier evidence, not evidence
   about a real host's swap): a transfer must be refused, listing every unsafe
   device, with a swap file, a disk partition, zram writing back to a disk, a
   non-crypt device-mapper device and an unknown zram device. All other phases
   see a `/proc/swaps` with no swap, through that copy; crash cases that use
   the committed crash classifier run from a copy whose only change is that
   swap stub.
   Crash dumps are checked the same way: when the classifier reports the
   machine's real `kernel.core_pattern` and SSH session core limit unsafe (WSL's piped
   crash handler is), a transfer with them must be refused. Then
   when that real pattern is systemd-coredump with a literal limit argument
   (this WSL machine's is), a transfer with it under a session limit of 0 must
   be refused. A transfer with a `core` file pattern bind-mounted over
   `/proc/sys/kernel/core_pattern` must be refused by the committed role, as a
   masked sysctl. For the remaining cases
   `kernel.core_pattern` is bind-mounted inside each host's mount namespace and
   the role runs from the synthetic test-only copy, whose crash classifier
   differs only in its `kernel_pattern` check, which always passes; its results are synthetic classifier evidence, not
   evidence about a real host's crash policy. Transfers must be refused,
   giving the reason, for Apport, for core files
   with a nonzero limit, for systemd-coredump storing dumps with a nonzero
   limit, for systemd-coredump with `Storage=none` but the default or a
   nonzero `ProcessSizeMax`, for the disabling settings outside `[Coredump]`
   or in a drop-in without a section header, for a path-like comment before
   settings that re-enable dumps, for a UTF-8 byte order mark, for a malformed
   section header, for a line continuation, for the disabling settings only
   in a main `coredump.conf` in `/usr/local/lib`, for systemd-coredump with a
   literal limit argument, persistent storage and a session limit of 0, and
   for `@` and `@@` socket collectors. Because the test hosts are
   containers, which do not share the host's initial namespaces, transfers
   must also be refused, as unverifiable, for systemd-coredump with
   `Storage=none` and `ProcessSizeMax=0`, for systemd-coredump with a session
   limit of 0 and for `/usr/bin/false`; a test-only copy of the classifier
   whose namespace check always passes must accept those three, showing the
   namespace check is the only reason they were refused
   (the containers' init and sshd inherit Docker's
   unlimited limit). No test
   process is crashed: `kernel.core_pattern` is global to the shared kernel,
   so a real crash would reach the machine's own handler. Enrollment runs with
   core files under a `limits.d` core limit of 0 for the deploy account, through
   the same synthetic copy, because that file pattern is a bind-mounted fixture.
   Then a claim transfer is
   interrupted right after staging, for plain and Vault claims, with zram
   without writeback and dm-crypt swap and with core files under a
   `limits.d` core limit of 0 for the deploy account (all accepted): the claim must
   exist only in the tmpfs staging directory, not anywhere on the host's
   persistent filesystem (including Ansible's remote temporary directories)
   or in the controller's files and Ansible home, and no transfer copy may
   remain in `/dev/shm`. No claim may appear in the sudo I/O logs, after this
   phase or after enrollment.
4. Install, on-device identity, enrollment, configuration and start, in three
   rollout batches.
5. A rerun with the credential batch deleted reports `changed=0`, and so does
   check mode. Then staging is planted as if enrollment stopped after writing
   its record but before its cleanup, together with an abandoned transfer
   directory of the SSH account and a same-named directory of another
   account: check mode reports it without erasing, the retry (still without
   the batch) erases the staging and the SSH account's transfer copy, keeps
   the other account's directory, and neither re-enrolls nor restarts a
   service; a rerun reports `changed=0`.
6. An upgrade preview in check mode leaves bytes, package versions and service
   processes unchanged.
7. Package and binary upgrade, then a rerun with `changed=0`.
8. An interrupted binary/configuration rollout: the files land but no restart
   runs. The retry must restart the stale services onto the installed binary
   and configuration, then a rerun reports `changed=0`.
9. A configuration-only change whose file time is set to half a second after
   the daemon started (indistinguishable by timestamp) is detected; the retry
   restarts the daemon onto it, then a rerun reports `changed=0`.
10. A signing socket unit-only update (`SocketMode` 0660 to 0620) on the
   binary-profile host, interrupted after the unit files and daemon-reload:
   the retry must recreate the socket with the new mode and restart the
   signer and the daemon, then a rerun reports `changed=0`.
11. Rolling restart.

It checks key, address and enrollment preservation, daemon restarts and journal
initialization. On chain, each enrollment must be device-signed and paid by that
device's own claim, with the DeviceCap owned by the device and the
DeviceOwnerCap by the operator. It also checks claim erasure, file modes, no
leftover Ansible temporary files, and that no device or claim key appears in
Ansible output, controller files or host journals, and that no claim appears
on any host's persistent filesystem. The scanners receive keys on standard
input, never as process arguments.

**Limits.** The containers are not physical targets. They share the host
kernel (Linux 6.6 on the test host), and arm64 was not run. The harness checks
check-mode, idempotence and enrollment behavior on a disposable chain. It does
not cover public-network enrollment, real fleet scale, physical boot or
reboot, power loss, hardware signers or production package publication.
Physical acceptance stays in SF-031 ([#32](https://github.com/tyler-suifleet/suifleet/issues/32)).
Mainnet and production-provider acceptance stay in SF-087
([#159](https://github.com/tyler-suifleet/suifleet/issues/159)). Production
hosting and signing stay in SF-042 ([#62](https://github.com/tyler-suifleet/suifleet/issues/62)).

**Recorded run (2026-11-24, after review repair).** `scripts/test_ansible.py`
passed every phase with ansible-core 2.20.11 and client 0.2.3 (debug build,
stripped): the six preflight cases, the upgrade preview (bytes, package
versions and service PIDs unchanged), the interrupted rollout and its retry
(both services on the binary-profile host and the daemons on the `deb` hosts
restarted onto the installed inputs), and converged reruns and check mode with
`changed=0` on all three hosts. The three enrollments succeeded on the
disposable chain, each paid by its own claim. The run used no public network
or testnet funds. A rerun on 2026-11-25 with sudo input logging enabled on
every host passed all phases (30 Ansible runs): the control marker sent
through become was recorded in each host's `/var/log/sudo-io`, and no claim
appeared there after the interrupted transfer, after enrollment or in the
final scan.
