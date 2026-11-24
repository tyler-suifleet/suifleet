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
standard Debian and Ubuntu installs) for the `apt` module. Use a controller with
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
Before starting services, the role checks each running SuiFleet service on the
host: a process whose executable was replaced, whose unit files changed, or
that started before the files it reads (`suifleet_signer_inputs`,
`suifleet_daemon_inputs`) is restarted. A rollout that stopped after its files
landed but before the restarts therefore converges on the next run, and the
health check fails while a service still runs replaced inputs.

## Security properties

- **Device keys never leave the device.** Each host generates its own key; the
  role checks only the key's owner and mode. It never reads, copies or fetches
  the key, and never puts a key in an image, package or Git.
- **Claims are single-use and kept private.** The claim key is copied with
  `no_log` into a private staging directory on a memory-backed `/run`. It is
  used by the current key holder (the daemon user, not root), then erased.
  Ansible's own transfer passes through the connection user's private
  temporary directory. Revoke a claim that was exposed before use.
- **One enrollment per device.** `/etc/suifleet/enrollment.json` records the
  public IDs, and a recorded host never enrolls again. A host that has
  `config.toml` but no record is refused rather than given a new claim.
- **Records are checked against the key.** A record whose address differs from
  the device's own key stops the play.
- **No secrets in command arguments or logs.** Commands receive file paths
  only. `ansible.cfg` sets no log file. The daemon journal stays on the host;
  only health exit codes are reported.
- **Least privilege.** Connect as an unprivileged account with sudo; never SSH
  as root. Package installation, service control and file ownership need root.
  The daemon and the signing service run as separate unprivileged users
  ([key isolation](KEY_ISOLATION.md)). Firmware helpers are not installed.

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

The harness checks these phases:

1. Profile selection with injected facts: Debian 12.x and Ubuntu 24.04 select
   `deb`; Ubuntu 24.10 and Debian 13 are refused for `deb` unless explicitly
   allowed.
2. Refusal of the host without systemd.
3. Check mode on fresh hosts changes nothing.
4. Install, on-device identity, enrollment, configuration and start, in three
   rollout batches.
5. A rerun with the credential batch deleted reports `changed=0`, and so does
   check mode.
6. An upgrade preview in check mode leaves bytes, package versions and service
   processes unchanged.
7. Package and binary upgrade, then a rerun with `changed=0`.
8. An interrupted binary/configuration rollout: the files land but no restart
   runs. The retry must restart the stale services onto the installed binary
   and configuration, then a rerun reports `changed=0`.
9. Rolling restart.

It checks key, address and enrollment preservation, daemon restarts and journal
initialization. On chain, each enrollment must be device-signed and paid by that
device's own claim, with the DeviceCap owned by the device and the
DeviceOwnerCap by the operator. It also checks claim erasure, file modes, no
leftover Ansible temporary files, and that no device or claim key appears in
Ansible output, controller files or host journals. The scanners receive keys
on standard input, never as process arguments.

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
or testnet funds.
