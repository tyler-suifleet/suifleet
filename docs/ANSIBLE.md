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
controller, then uploads the artifact to a private per-run file under
`/var/cache/suifleet` and checks that copy on each host. Only a matching copy
is renamed to the cache path named by its pinned SHA-256, from which it is
installed; an existing file at that path is reused only after the same check.
A binary replaces `/usr/bin/suifleet` only from that verified file. So the
cache path never holds unverified bytes, even when overlapping rollouts use
the same or different pins or a controller's artifact changes after its
preflight check: a mismatching upload is removed and its run stops. A run
removes other releases' staged files (and abandoned uploads a day old) after
installing, which makes an overlapping rollout of another release fail at its
install rather than install other bytes. Overlapping `site.yml` runs on one
host do not interleave at all: each holds the host's
[rollout lock](#recovery) from before its installation through its final
health check and applied-inputs record, and a second run, of any release or a
preview, stops before it uploads or changes anything. A rolling restart
(`restart.yml`) takes the same lock, so it neither restarts a host during a
rollout nor lets a rollout start during its restart and checks. So a run never
restarts, verifies or records another run's release as its own. A preview
checks the lock only once, when it starts; a real run that starts after that
check is not excluded, and the preview may then read inputs that run is
changing. Preview when no rollout or restart targets the hosts. Restoring only the cache file of
the release already installed restarts nothing; services restart only for an
installed change or inputs not yet applied. (A `deb` preview cannot ask apt
about a package it has not uploaded, so it reports that restore as a pending
installation; the preview itself changes nothing. A `systemd-binary` preview
compares the installed `/usr/bin/suifleet` with what the install enforces, the
pinned bytes in a regular `root:root` file with mode `0755`, so drifted
metadata previews as an installation and a restart of both services. A
directory, or a symlink to one, at `/usr/bin/suifleet` stops the host before
anything is uploaded, in a preview or a real run.) For
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
| `suifleet_enrollment_root` | group | The extracted credential batch on the controller: an absolute path without `$`, which Ansible would expand to another folder. |
| `suifleet_deb_src`, `suifleet_deb_sha256` | group | The verified package and its SHA-256 (`deb`). |
| `suifleet_binary_src`, `suifleet_binary_sha256`, `suifleet_units_src` | group | The verified binary, its SHA-256 and the unit files (`systemd-binary`). |
| `suifleet_profile` | any | `auto`, `deb` or `systemd-binary`. |
| `suifleet_apply_command` | group | Your reviewed platform installer. The default `/usr/bin/false` installs nothing. |
| `suifleet_apply_timeout_seconds`, `suifleet_apply_allow_recipe_commands`, `suifleet_apply_mode`, `suifleet_poll_seconds`, `suifleet_replay_history` | group | The matching `config.toml` settings. |
| `suifleet_config_extra`, `suifleet_signer_extra` | group | Extra reviewed TOML for `config.toml` and `signer.toml`. Each block starts with its own `[table]`. Never put secrets here. |
| `suifleet_rollout_batches` | play | Rollout batches. Default `[1, "25%", "100%"]`: one canary host, then a quarter, then the rest. |
| `suifleet_max_fail_percentage` | play | Default `0`: any failed host stops later batches. |
| `suifleet_hosts` | play | Inventory group. Default `suifleet_devices`. |
| `suifleet_revocation_digest` | host | Only to settle an enrollment attempt that printed no receipt: the digest of the revocation of its recorded claim ([recovery](#recovery)). Remove it afterwards. |

Every controller path (`suifleet_manifest_src`, `suifleet_deb_src`,
`suifleet_binary_src`, `suifleet_units_src`, `suifleet_enrollment_root`) must be
absolute and contain no `$`; the role refuses others before reading anything.
Ansible would resolve a relative or `$` path to one file for the checksum and
another for the copy. The role also checks each copy of the manifest on the host
against the controller's SHA-256. The installed `/etc/suifleet/deployment.json`
is replaced only after the transferred bytes pass that check, so a controller
file that changes during a run fails it and leaves the previous manifest in place.

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
`suifleet_claim_transfer_dir`), no active swap (not even zram) and no crash
handler that can store a core dump while they enroll
([claim storage](#claim-storage)). Use a controller with
ansible-core 2.16 or later. The examples use only
`ansible.builtin` modules. The tested controller is pinned in
[`requirements-controller.txt`](../examples/ansible/requirements-controller.txt)
for CPython 3.12 on x86_64 Linux; create the virtual environment with that
interpreter, then activate it so `ansible-playbook` resolves to the pinned
copy rather than any other Ansible on your `PATH`:

```sh
cd examples/ansible
python3.12 -m venv ~/.venvs/suifleet-ansible
. ~/.venvs/suifleet-ansible/bin/activate
pip install --require-hashes -r requirements-controller.txt
ansible-playbook -i ~/fleet/inventory.yml site.yml --check --diff   # preview
ansible-playbook -i ~/fleet/inventory.yml site.yml                  # install, enroll, start
```

Run later `ansible-playbook` commands in a shell where this environment is
active (`. ~/.venvs/suifleet-ansible/bin/activate`).

After enrollment, run the Enrollment page's pending transactions again. They
bind each enrolled claim to its serial, alias and group. Then fund device gas
with *Fund enrolled devices*. Ansible holds no wallet key and sends no
transactions other than each device's own claim-sponsored enrollment.

**Upgrade.** Change the artifact path and SHA-256, then run `site.yml` again.
The package stops the units, and the role starts them again batch by batch.
Keys, address, enrollment record, configuration and journal are preserved.

**Restart.** `ansible-playbook -i INVENTORY restart.yml` restarts the daemon
one host at a time. It checks health and that the address did not change.
Each host restarts under the [rollout lock](#recovery), held from before the
restart through these checks; a host whose lock another play holds stops the
restart before anything restarts.
With `--check` it restarts nothing and skips the post-restart health checks,
which could only validate a real restart; it still reads the address.

**Idempotence.** Rerunning `site.yml` on converged hosts reports no changes.
Check mode reports pending installs, upgrades and enrollments without
performing them; an upgrade preview leaves the installed bytes, package and
running services untouched. Because a preview starts nothing, it reads the
daemon state once and reports a stopped daemon instead of waiting for it, and
skips the journal-initialization and running-inputs checks. It never runs the
read-only `--self-check`: the check connects to the signing socket and could
start the signing service through socket activation, even one that was running
when the preview read its state and stopped afterwards. A configured host whose
units are stopped (for example after an interrupted upgrade) therefore previews
successfully, and the real run performs every post-start check, including the
self-check.

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
the restarts therefore converges on the next run. Systemd is also reloaded
before any unit starts whenever a unit, active or stopped, has unit-file
changes it has not loaded, so a run that stopped between installing unit
files and its daemon-reload never starts a unit from the cached definition.
The provisioning oneshot is covered as well: its pending definition is loaded
before the identity step starts it. A unit's file paths (fragment and
drop-ins) are only known once systemd has loaded them, and a reload loads
every changed definition, including one whose unit never reported a pending
reload. After any reload the role therefore samples the units again: a unit
whose loaded definition differs from the record is restarted, staleness found
before the reload is kept, and the health check compares against these loaded
digests; the record stores the verified digests. A drop-in added or removed
before a run is therefore applied and recorded rather than reported as a
change during the rollout. systemd (252 and 255 were tested) does not report
a newly added drop-in as needing a reload unless another unit file of that
unit changed. Such a drop-in is applied by the next run that reloads systemd
for any reason (for example a configuration change); to apply it in a run
that changes nothing else, run `systemctl daemon-reload` after adding it by
hand. A service whose main process exits while it is inspected is sampled again
(bounded), and a sample is used only if the main process and invocation are
unchanged after its executable was read.

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
  carries only paths, then copies it into a staging directory on a
  memory-backed `/run`, and the transfer directory is erased. Root never
  opens the transferred file itself, since the SSH account can replace it
  at any time: a reader running as the SSH account opens it without
  following symlinks or blocking, accepts only a regular single-link file
  of at most 4 KiB that the account owns, and passes the bytes it read to
  root through a pipe; it refuses to write to anything but a pipe. Root
  stops reading as soon as more than 4 KiB arrive and gives the reader 60
  seconds to finish; otherwise it kills the reader and stages nothing, so a
  misbehaving SSH account cannot hold the host's rollout lock. Root
  creates the staged copy only after the reader exited successfully, so the
  reader never holds a handle to it and a refused or failed read stages
  nothing. Root owns the
  staging directory (mode 0750, the key holder's group), the `enroll.toml`
  and `deployment.json` staged in it (mode 0640) and `claim.key` (mode 0400,
  unreadable to the key holder). The key holder (the daemon user, not root)
  runs the address derivation, the enrollment command and the on-chain
  lookup. It can read the configuration and manifest but cannot create,
  rename, replace or change any staged input, so the command reads the
  manifest whose SHA-256 the role verified and recorded. Root pipes the one
  staged `claim.key` to both the derivation and the enrollment command (the
  staged `enroll.toml` reads the claim from `/dev/stdin`), so the claim
  address the attempt records is the claim the command sends. Each key-holder
  process sets a core dump size limit of 0 after `runuser`'s PAM limits
  apply, before it reads the claim. Ansible's file copy,
  which goes through the remote temporary directory, is never used for the
  claim. Enrollment refuses to send a claim unless modules are pipelined, so
  keep `pipelining = True` and leave `ANSIBLE_KEEP_REMOTE_FILES` unset. The
  staging directory is erased after enrollment, when it fails, and at the
  start of every run, before any enrollment check, so a run that refuses the
  host (for example an unverifiable interrupted receipt) still erases a claim
  left by a killed run, as does a run of an enrolled host. Every run also
  erases the SSH account's transfer directories left by a killed run. On a
  host that is not enrolled, that cleanup and the enrollment run under the
  host's [enrollment lock](#recovery), so a run never erases the claim of
  another play that is enrolling the same host. An interrupted
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
`/proc/swaps` and refuses the host if it lists **any** active swap; the
message names the entries. Hibernation writes memory to swap, so it is
covered by the same rule.

This includes swap that looks memory-only or encrypted, such as zram without
a writeback device or dm-crypt swap. `/proc/swaps` names an entry only by the
path it was opened through in the namespace that ran `swapon`. That text does
not identify the block device: a node can be given any name, for example a
disk node called `/dev/zram0`. No unprivileged kernel interface binds a swap
entry to a device number, so attributes read from `/sys/block` under that
name (`backing_dev`, `dm/uuid`) cannot be shown to describe the device in use.

The session's view of the swap list can be substituted: a file or an
LXCFS-style FUSE view bind-mounted over `/proc/swaps` can list no swap while
the kernel swaps to a disk. So the role reads it only once
`/proc/self/mountinfo` shows that `/proc/swaps` is served by procfs at
`/swaps`. Otherwise, or when the mount table cannot be read, the host is
refused as unverifiable before the list is read. Enroll from a session that
sees the kernel's `/proc`, not from inside a container that masks it.
The list itself is parsed by the shell alone, without a pipe through another
tool, so a failed or missing tool cannot make active swap look like none; a
list without its header, or one the parser cannot account for, is refused as
unreadable.

For a host with swap, run `swapoff -a` before enrolling it and `swapon -a`
afterwards (recorded hosts never handle a claim again). Disable zram swap
the same way (`swapoff /dev/zramN`).

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
- **Interrupted enrollment.** The enrollment command prints its receipt
  (public IDs) only after the transaction succeeded, and the claim is then
  spent. If a run stopped after that but before the record was renamed into
  place, `/etc/suifleet/.enrollment.json.partial` holds the receipt. The next
  run never submits again or overwrites it: it restores the record from a
  complete receipt for the device's own key, without needing the credential
  batch. Any other non-empty content (for example a truncated file, or a
  receipt for another key) stops the run before the claim is staged.
  The same key can enroll under another deployment, or under another
  enrollment policy of the same deployment (each creates its own Device and
  DeviceCap), so each attempt also records the SHA-256 of the manifest it
  enrolled under and its policy ID (two lines: the digest, then the policy ID
  as `0x` and 64 lowercase hex digits) in
  `/etc/suifleet/.enrollment.json.partial.deployment`, durably and before
  anything is submitted. A receipt is promoted only while
  `suifleet_manifest_src` still has those bytes. If the inventory now selects
  another deployment, or no deployment is recorded, the run keeps the receipt
  and stops before writing a record or any configuration (check mode too).
  With another deployment recorded, select the deployment the device enrolled
  under: the receipt and the binding belong to it, and the procedure below
  refuses to restore a record under any other. With none recorded, restore the
  record under the selected deployment by hand as below; the lookup checks that
  the device enrolled under it, and the procedure records it.

  The binding outlives the promotion: it stays beside
  `/etc/suifleet/enrollment.json` until the run has installed `config.toml`,
  and only then is removed. A run stopped after the record was promoted but
  before configuration finished (or whose manifest failed its digest check)
  therefore resumes only under the deployment the device enrolled under,
  without enrolling again; with another deployment selected it stops before
  installing `deployment.json`, `signer.toml` or `config.toml` (check mode
  too). A record with neither a binding nor `config.toml` also stops the run:
  after confirming which deployment and enrollment policy the device enrolled
  under, record that manifest's SHA-256 and the policy ID as root (here from
  a copy of the manifest in the current directory, which must be the file
  `suifleet_manifest_src` names, and the host's `enroll.toml` from its batch
  folder) and run again:

  ```sh
  { sha256sum < deployment.json | cut -c1-64
    python3 -I -c 'import sys, tomllib; p = tomllib.load(open(sys.argv[1], "rb"))["policy_id"]; print("0x" + p[2:].lower().rjust(64, "0"))' enroll.toml
  } > /etc/suifleet/.enrollment.json.partial.deployment
  ```

  On a configured host (the binding removed, `config.toml` present) a later
  manifest change, such as a package upgrade, applies as usual.

  The role creates the partial file exclusively, and flushes it to disk,
  before it runs the enrollment command, so every attempt leaves it. An empty
  partial file means the attempt printed nothing, and its outcome is unknown:
  the command prints only after its transaction committed, so a lost response
  or a host stopped at the wrong moment leaves an enrolled device with no
  receipt. The next run then looks the enrollment up on chain with
  `suifleet --find-enrollment`, a read-only command that never reads the claim
  or submits anything. It uses the host's folder in the credential batch
  (`enroll.toml` and `deployment.json`; `claim.key` is not used), runs as the
  device key's holder, and finds the `DeviceEnrolled` event that this device
  key sent under the batch's policy, checks that the DeviceCap is owned by and
  bound to the key and that the registry still trusts it, and prints the same
  receipt as the enrollment command. The role verifies that receipt like any
  other and restores the record from it, keeping the device's key and
  identity. It never enrolls again. The same key can hold an enrollment under
  another deployment or policy, so before the lookup the role first requires
  the attempt's binding (`/etc/suifleet/.enrollment.json.partial.deployment`)
  to name `suifleet_manifest_src` and, after checking the batch folder below,
  the policy of the folder's `enroll.toml`. A binding for another deployment
  or another policy, one without a policy line (written by an earlier
  version of the role), or none, stops the run (check mode too) before
  anything is looked up, keeping the empty file and the binding unchanged:
  select the deployment and batch folder the attempt enrolled under, or
  settle it as below. It then requires the folder's
  `deployment.json` to be identical to `suifleet_manifest_src` (the manifest
  it installs beside the restored IDs) and its `enroll.toml`, as parsed TOML,
  to name that batch-relative manifest and `/etc/suifleet/device.key`, with no
  `[signer]` table or other unexpected field, exactly as for a fresh
  enrollment. The role then stages a configuration rebuilt from those checked
  values and `suifleet_manifest_src`, not the folder's own files. A stale or
  misselected folder stops the run (check mode too) before anything is copied
  to the host, keeping the empty file and writing no record.

  To restore a record by hand (for example after a truncated receipt, or a
  configured host with no record), first make sure no play targets the host.
  As root, copy the host's `enroll.toml` and `deployment.json`, but not
  `claim.key`, into `/root/lookup`, together with a copy of the manifest
  `suifleet_manifest_src` names, as `fleet.json`. `/root/lookup` must be a
  directory that only root can write (root's home is 0700 by default); do not
  put the inputs anywhere the key holder can write. Then run the block below as
  root. It needs Python 3.11 or later (for `tomllib`) on the host; Debian 12 and
  Ubuntu 24.04 have it. The restored IDs are used with `fleet.json`, so the
  lookup's `deployment.json` must describe the same deployment (only its node
  URLs, `rpc_url` and `rpc_urls`, may differ, as below), and `enroll.toml` must
  use it and the device's own key.

  The block does not read the inputs in place. It copies them, as regular
  files that are not symlinks, into a fresh workspace that root creates under
  `/run` (`mktemp`, owned by root, mode 0750 with the key holder's group). The
  copied files are root-owned with mode 0640, so the key holder can read them
  but cannot replace or change them. It then validates those copies, and the
  lookup reads the same bytes. The lookup's output is captured by root after
  the command exits and written into that workspace, which the key holder
  cannot write, before it is validated and installed. The workspace is removed
  on exit; remove `/root/lookup` yourself afterwards.

  ```sh
  sh -eu <<'EOF'
  src=/root/lookup
  binding=/etc/suifleet/.enrollment.json.partial.deployment
  test ! -e /etc/suifleet/enrollment.json
  holder=$(stat -c %U /etc/suifleet/device.key)
  dir=$(mktemp -d /run/suifleet-lookup.XXXXXX)
  trap 'rm -rf "$dir"' EXIT
  chgrp "$(id -gn "$holder")" "$dir"
  chmod 0750 "$dir"
  python3 -I -c 'import os, stat, sys
  src, dst = sys.argv[1:3]
  for name in ("enroll.toml", "deployment.json", "fleet.json"):
      fd = os.open(os.path.join(src, name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
      with os.fdopen(fd, "rb") as f:
          if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
              sys.exit(name + " is not a regular file")
          data = f.read()
      out = os.open(os.path.join(dst, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
      with os.fdopen(out, "wb") as f:
          f.write(data)' "$src" "$dir"
  for name in enroll.toml deployment.json fleet.json; do
    chgrp "$(id -gn "$holder")" "$dir/$name"
    chmod 0640 "$dir/$name"
  done
  policy=$(python3 -I -c 'import json, re, sys, tomllib
  def unique(pairs):
      if len({k for k, _ in pairs}) != len(pairs):
          sys.exit("duplicate key in a manifest")
      return dict(pairs)
  manifests = [json.load(open(sys.argv[1] + name), object_pairs_hook=unique)
               for name in ("/deployment.json", "/fleet.json")]
  for m in manifests:
      if not isinstance(m, dict):
          sys.exit("not a manifest")
      m.pop("rpc_url", None), m.pop("rpc_urls", None)
  if manifests[0] != manifests[1]:
      sys.exit("deployment.json describes another deployment than fleet.json")
  c = tomllib.load(open(sys.argv[1] + "/enroll.toml", "rb"))
  if (c.get("manifest_path") != "deployment.json" or "signer" in c
          or c.get("keypair_path") != "/etc/suifleet/device.key"):
      sys.exit("enroll.toml must use deployment.json and /etc/suifleet/device.key")
  p = c.get("policy_id")
  if not isinstance(p, str) or not re.fullmatch("0x[0-9a-fA-F]{1,64}", p):
      sys.exit("enroll.toml must name an enrollment policy ID")
  print("0x" + p[2:].lower().rjust(64, "0"))' "$dir")
  want=$(sha256sum < "$dir/fleet.json" | cut -c1-64; printf '%s\n' "$policy")
  if test -e "$binding" && test "$(cat "$binding")" != "$want"; then
    echo "the attempt enrolled under another deployment or policy: use its manifest as fleet.json and its batch folder" >&2
    exit 1
  fi
  address=$(runuser -u "$holder" -- /usr/bin/suifleet --derive-address /etc/suifleet/device.key)
  receipt=$(runuser -u "$holder" -- /usr/bin/suifleet --find-enrollment "$dir/enroll.toml")
  (umask 077 && printf '%s\n' "$receipt" > "$dir/receipt.json")
  python3 -I -c 'import json, re, sys
  text = open(sys.argv[1]).read()
  r = json.loads(text) if re.fullmatch(r"\{[^\n]*\}\n?", text) else {}
  ids = [r.get(k, "") for k in ("device_object_id", "device_cap_id", "owner_cap_id")]
  ok = (r.get("device_address") == sys.argv[2]
        and all(re.fullmatch("0x[0-9a-f]{64}", str(i)) for i in ids)
        and re.fullmatch("[1-9A-HJ-NP-Za-km-z]{32,44}", str(r.get("digest", ""))))
  sys.exit(0 if ok else "not a complete receipt for " + sys.argv[2])' "$dir/receipt.json" "$address"
  if test ! -e "$binding" && test ! -e /etc/suifleet/config.toml; then
    (umask 077 && printf '%s\n' "$want" > "$binding.new")
    sync -f "$binding.new"
    mv "$binding.new" "$binding"
  fi
  install -m 0640 -o root -g suifleet "$dir/receipt.json" /etc/suifleet/.enrollment.json.restore
  sync -f /etc/suifleet/.enrollment.json.restore
  ln /etc/suifleet/.enrollment.json.restore /etc/suifleet/enrollment.json
  rm -f /etc/suifleet/.enrollment.json.restore /etc/suifleet/.enrollment.json.partial
  EOF
  ```

  Before it changes anything it refuses an input in `/root/lookup` that is a
  symlink or not a regular file, an attempt binding for another
  deployment than `fleet.json` or another policy than `enroll.toml`'s (or one
  without a policy line), a lookup `deployment.json` that differs from
  `fleet.json` in more than its node URLs, and an `enroll.toml` that names
  another manifest or key or has a `[signer]` table. It then installs the
  record, and only then removes the attempt marker, after the lookup
  succeeded and printed a complete receipt for the device's own key. An
  existing binding (necessarily for `fleet.json`) is kept; on an unconfigured
  host without one it first records `fleet.json`'s SHA-256 and the policy ID
  as the binding, so the next run configures the host only under the
  deployment the lookup found the enrollment in. A configured host gets no binding, as after a completed
  run. Any failure (a conflicting binding or folder, a lookup that found
  nothing, a receipt for another key, an existing record) stops it with a
  non-zero status, installs nothing and keeps
  `/etc/suifleet/.enrollment.json.partial`. The next run checks the record
  against the device key and the binding against the configured deployment
  again.

  The lookup exits with status 3 when the node's retained history holds no
  enrollment by this key. That does not settle an attempt: the history may be
  pruned, and the enrollment transaction has no expiration, so an attempt that
  has not committed yet (for example one still pending at the node) may still
  commit later. The run then stops before staging a claim or submitting again,
  and keeps the file until the chain settles the outcome. Only a revocation of
  the claim that attempt used, under the policy it used, settles it. Another
  serial's claim, or the same serial's claim under another policy or job,
  leaves the attempt able to commit. Each attempt therefore also records the
  public address of its claim in `/etc/suifleet/.enrollment.json.partial.claim`
  (one line), durably and before anything is submitted. The stop message
  names that claim and the recorded policy.

  1. On the Enrollment page, connected to the network the deployment names,
     select the enrollment policy the stop message names (the policy line of
     `/etc/suifleet/.enrollment.json.partial.deployment`). Revoke the claim
     (*Lost a credential file?*, then *Revoke lost claims* for the host's
     serial). With *Technical details* on, the serial's claim must start like
     the recorded one. Revocation removes the claim from the policy in a
     transaction ordered with the enrollment, so it either succeeds, and the
     earlier attempt can then never commit, or fails. A failure alone does not
     show that the attempt committed (the transaction may also fail for gas, a
     network error or a stale page), so keep the file. Reload the page: if the
     serial now shows *Consumed, pending binding* or *Bound*, the attempt used
     the claim. A node that pruned the event still cannot find it, so run the
     lookup against a trusted full node of the same network that retains it:
     set `rpc_url` (and `rpc_urls`) in `/root/lookup/deployment.json` to that
     node (the lookup checks the chain identifier) and restore the record by
     hand as above. If no trusted node retains the event, keep the file and
     escalate; never remove it, issue a new claim or enroll again. If the
     serial still shows *Funded, unused*, retry the revocation.
  2. Once the revocation succeeded, take its transaction digest (from the
     wallet's activity, or the `log` of the job file from *Export job*), set
     it for that host only and run the play again:

     ```sh
     ansible-playbook site.yml --limit HOST -e suifleet_revocation_digest=DIGEST
     ```

     The role runs the lookup again and, only if it still finds no
     enrollment, runs the read-only `suifleet --check-revocation` as the key
     holder, with the same verified `enroll.toml` and manifest (the chain
     identifier, package and policy are checked as for the lookup). It
     requires that the transaction succeeded and is in a checkpoint, that it
     deleted the recorded claim's entry in the recorded policy's claim table
     (the entry's object ID is derived from both, so a revocation of another
     claim or under another policy does not match), that it emitted no
     `DeviceEnrolled` event (an enrollment also deletes the entry, and
     revocation aborts unless the claim was unused), and that the claim is
     still not registered. Only then does it remove the empty file and the
     binding, and stop. A missing or malformed claim record (an attempt by an
     earlier version of the role) or a digest that fails any check keeps the
     file and the binding; escalate rather than remove them. The claim record
     stays, and the role refuses to enroll with that claim again.
  3. Reissue the serial (*Remove reservations to reissue*, reserve it again
     and download its new folder into the credential batch), remove
     `suifleet_revocation_digest` and run the play again.

  Never remove the file by hand: not because the page shows *Funded, unused*
  or *Revoked*, nor because the lookup found nothing. The page describes the
  chain as last read (an open page refreshes it only once a minute) and the
  job and policy it has selected, and neither describes an attempt that may
  still commit. Never re-register the revoked claim
  (*Re-register withdrawn claims*): registering it again would let that
  attempt commit (the check refuses a claim registered again, but cannot see
  an attempt that already used the re-registered claim).
  Never issue an enrolled device a new claim, and never replace its key.
- **Enrollment lock.** One play at a time enrolls a host: it holds
  `/run/suifleet-enroll.lock` from the cleanup of an interrupted run's claim
  copies until the receipt is promoted, so overlapping plays never share the
  claim staging, the attempt marker or a submission. Another play stops
  without changing anything. A play killed while enrolling leaves the lock;
  the next run stops on it. Once no play targets the host and no
  `suifleet --enroll` process runs on it, remove it as root (a reboot also
  clears it), then follow the steps above for any partial file it left:

  ```sh
  rm -r /run/suifleet-enroll.lock
  ```
- **Rollout lock.** One play at a time changes a host. A `site.yml` play
  holds `/run/suifleet-rollout.lock` from before the installation until the
  final health check has recorded the applied inputs; a `restart.yml` play
  holds it from before the restart through its health and identity checks.
  Another play (`site.yml` or `restart.yml`, a run or a check-mode preview, of
  the same or another release) stops on it before it uploads, restarts or
  changes anything. A preview looks for the lock only when it starts. A play
  killed while holding the lock leaves it; the next run stops on it. Once no `ansible-playbook` targets the host,
  remove it as root (a reboot also clears it) and run `site.yml` again, which
  converges the host to the inventory's pins:

  ```sh
  rm -r /run/suifleet-rollout.lock
  ```
- **Swap refusal.** See [claim storage](#claim-storage): enroll with all
  swap off, including zram and encrypted swap. Nothing was sent.
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
   transfer with it unchanged must be refused, naming the device. A machine
   without swap skips that case. Then `/proc/swaps` (and, for one case,
   `/sys/block`) are replaced inside each host's mount namespace; nothing is
   swapped on or off. The committed role must refuse a transfer, as
   unverifiable, with a header-only file bind-mounted over `/proc/swaps`. The
   remaining swap fixtures run through a test-only copy whose swap
   classifier's `kernel_view` check always passes (synthetic classifier
   evidence, not evidence about a real host's swap). A transfer must be
   refused, naming both devices, with `/dev/zram0` and `/dev/dm-0` listed and
   zram `backing_dev` `none` and a dm-crypt UUID on a tmpfs over `/sys/block`:
   the names do not identify the devices, so safe-looking attributes are not
   trusted. A transfer must also be refused, listing every entry, with a swap
   file, a disk partition, zram writing back to a disk, a non-crypt
   device-mapper device and an unknown zram device. All other phases see a
   `/proc/swaps` with no swap, through that copy; crash cases that use the
   committed crash classifier run from a copy whose only change is that swap
   stub.
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
   interrupted right after staging, for plain and Vault claims, with no
   swap and with core files under a
   `limits.d` core limit of 0 for the deploy account (all accepted): the claim must
   exist only in the tmpfs staging directory, not anywhere on the host's
   persistent filesystem (including Ansible's remote temporary directories)
   or in the controller's files and Ansible home, and no transfer copy may
   remain in `/dev/shm`. No claim may appear in the sudo I/O logs, after this
   phase or after enrollment.
4. On one host, a test-only copy of the role runs a stand-in for the
   enrollment command that waits, then fails without printing or submitting.
   It must start with the role's empty `.enrollment.json.partial` as its
   output, and as the key holder it must find the staging directory and its
   `enroll.toml`, `deployment.json` and `claim.key` (mode 0400) owned by
   root and unwritable (no file can be created, renamed or appended to).
   Under a test limits file that gives the key holder an unlimited core size
   through `runuser`'s PAM limits, the address derivation and the enrollment
   command must each report a core limit of 0 and must each have read,
   through a pipe, the bytes of the staged `claim.key`.
   While it waits, an overlapping check-mode run and an overlapping
   run stop on the enrollment lock without changing the first play's staged
   claim, lock or marker or reaching the enrollment command. After the
   failure the empty marker remains, the lock and staging are gone, and a
   retry stops on the marker. The attempt must have recorded its claim's
   address before the command ran. The chain setup revoked that claim under
   the attempt's policy, registered and revoked the same claim under the
   second policy, and registered and revoked another claim under the
   attempt's policy. Retries given the second and third revocations, or a
   transaction that revoked no claim, as `suifleet_revocation_digest` stop
   with the marker, binding and claim record unchanged. The first settles the
   attempt: the marker and binding are removed and the claim record is kept.
   A retry with the revoked claim still in the batch is refused before any
   marker exists; the serial then gets a new, funded claim (Vault-encrypted)
   and enrolls with it below. Then a test-only copy stops the enrollment right after
   the transaction succeeded and its receipt reached
   `.enrollment.json.partial`, before the record is promoted. With the
   credential batch present, retries with a truncated receipt, with a
   receipt for another key, and with an empty file (the committed enrollment
   printed nothing) stop before any claim is staged or the enrollment command
   runs, and keep the file unchanged. The interrupted attempt must have
   recorded its manifest's SHA-256 and its policy ID; the complete receipt with another
   deployment selected in `suifleet_manifest_src`, and with no recorded
   deployment, is refused in check mode and a retry, keeping the receipt and
   writing no record, `deployment.json`, `signer.toml` or `config.toml`. An
   empty file whose binding names another deployment than a batch and
   inventory that agree with each other, or with no binding, is refused before
   the lookup (check mode and a retry), keeping the file and the binding. So
   is an empty file whose binding names the same deployment but a second real
   policy that the chain setup created on the same registry, while the batch
   selects the policy under which the device key holds a valid enrollment, and
   one whose binding records no policy: no lookup runs, and no record or
   configuration is written. The real on-chain lookup, run through a test-only wrapper, must likewise find
   its staging directory and inputs owned by root and unwritable by the key
   holder. It restores the record in a play stopped before configuration;
   its deployment binding must remain, and with that promoted, unconfigured
   record another deployment is refused (check mode and a retry), as is a
   retry without the binding, before any configuration is written. With the
   real receipt restored and the batch removed, check mode reports the pending
   recovery without promoting it; a play stopped before configuration
   promotes it byte for byte, keeping the binding, and the retry under the
   enrolled deployment configures the host and removes the binding, without
   running the enrollment command again; key, address and enrollment are
   preserved, and the record is verified on chain with the others.
   Then install, on-device identity, enrollment, configuration and start run on
   all hosts in three rollout batches.
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
11. A unit-only update (`SocketMode` back to 0660) on the binary-profile host
   whose SuiFleet units are stopped but still enabled, interrupted after the
   unit files are installed and before the daemon-reload: the stopped units
   report a pending reload without being stale, and the retry must reload
   systemd before starting them (the socket comes up with the new mode and no
   unit waits for a reload), then a rerun reports `changed=0`.
12. A provisioning-unit update on the binary-profile host whose units
   (including the oneshot) are stopped, interrupted after the unit files are
   installed and before the daemon-reload: the retry must load the installed
   provisioning unit before starting it (a test-only privileged
   `ExecStartPost` marker proves which definition ran), identity is
   preserved, and a rerun reports `changed=0`.
13. A signing-socket drop-in (`SocketMode=0620`) added to a deb host without a
   daemon-reload (with the unit fragment reinstalled unchanged, as a package
   update leaves it), then removed the same way: each run loads the change,
   recreates the socket with the loaded mode, restarts the signer and the
   daemon and verifies against the loaded unit files; each rerun reports
   `changed=0`.
14. A signing-socket drop-in added to a deb host without a daemon-reload and
   without touching the fragment, so the socket reports no pending reload and
   matches its record. The next run changes only the daemon configuration;
   the role's reload for that change loads the drop-in, and the run must
   recreate the socket with its mode, restart the signer and the daemon and
   verify against the loaded unit files. Removing the drop-in while restoring
   the configuration is applied the same way; each rerun reports `changed=0`.
15. Check mode on configured hosts with their SuiFleet units stopped, as an
   interrupted upgrade leaves them, one host with only the signing socket
   still active and one host's journal not yet initialized: the preview
   succeeds, starts no unit (no socket-activated signer), dispatches no
   self-check and leaves the identity, enrollment, journal and applied record
   untouched. The converged and upgrade previews, where every signing service
   is active when the preview reads it, also dispatch no self-check. The real run then starts
   and verifies the hosts, and a rerun reports `changed=0`.
16. On each host, a test-only copy of the role rewrites the controller manifest
   after preflight hashed it and before it is copied: the copy fails its
   digest validation, the installed `deployment.json` stays byte for byte,
   no file is left in `/etc/suifleet`, no service restarts, and a rerun with
   the original manifest reports `changed=0`.
17. On each host, a test-only controller callback pauses a `site.yml` run
   right after its install task and, while it waits, runs a complete
   `site.yml` pinning another compatible release (deb `-3`, binary 2) as a
   check-mode preview and as a real run. Each overlapping play stops on the
   rollout lock before it uploads or installs anything; the first run then
   reports `changed=0`, restarts nothing, keeps its release, cache and
   applied-inputs record, and releases the lock. A planted stale lock stops a
   preview and a run without changes until it is removed; a rerun then
   reports `changed=0`.
18. Across playbooks: while a paused `site.yml` run holds a host's rollout
   lock after its install, a rolling restart (check mode and real) stops on
   the lock before restarting anything, and the rollout then reports
   `changed=0` with the same daemon and signer PIDs. While a paused
   `restart.yml` holds the lock after restarting the daemon, a `site.yml`
   check-mode preview and real run stop on it before any upload; the restart
   then completes its health and identity checks and releases the lock.
19. A rolling-restart preview in check mode succeeds without restarting the
   daemon or signer (same main PIDs) or changing the identity.
20. Rolling restart.

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
