#!/usr/bin/env python3
"""Prepare mounted removable media for SuiFleet first-boot enrollment."""

import argparse
from contextlib import closing, contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tomllib
import uuid

SERIAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
KEY = re.compile(rb"[0-9a-fA-F]{64}\n?")
REQUIRED = {"claim.key", "enroll.toml", "deployment.json"}
ALLOWED = REQUIRED | {"config.template.toml"}
MARKER = ".suifleet-media-id"
MARKER_PENDING = ".suifleet-media-id-pending"
STAGING = ".suifleet-enrollment-pending"
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class Refusal(ValueError):
    pass


def archive_index(archive):
    entries = {}
    names = set()
    for member in archive:
        path = member.name.split("/")
        folded = member.name.casefold()
        if folded in names or path[0] != "enrollment":
            raise Refusal("Duplicate or unexpected archive entry")
        names.add(folded)
        if member.isdir() and (path == ["enrollment"] or
                               len(path) == 2 and SERIAL.fullmatch(path[1])):
            continue
        if (not member.isfile() or len(path) != 3 or not SERIAL.fullmatch(path[1])
                or path[2] not in ALLOWED or not 0 < member.size <= 1024 * 1024):
            raise Refusal("Unexpected credential archive layout")
        entries.setdefault(path[1], {})[path[2]] = member
    if not entries or any(not REQUIRED <= files.keys() for files in entries.values()):
        raise Refusal("Incomplete credential archive")
    if len({serial.casefold() for serial in entries}) != len(entries):
        raise Refusal("Ambiguous archive serials")
    return entries


def read_credentials(archive, members):
    files = {name: archive.extractfile(member).read() for name, member in members.items()}
    if not KEY.fullmatch(files["claim.key"]):
        raise Refusal("Invalid claim key encoding")
    try:
        settings = tomllib.loads(files["enroll.toml"].decode())
    except ValueError:
        raise Refusal("Invalid enrollment TOML") from None
    if (settings.get("claim_key_path"), settings.get("manifest_path")) != (
        "claim.key", "deployment.json"
    ):
        raise Refusal("Enrollment input paths must refer to files in the serial folder")
    # Normalize the exported seed before deriving a non-reversible duplicate identifier.
    fingerprint = hashlib.sha256(bytes.fromhex(files["claim.key"].decode())).hexdigest()
    return files, fingerprint


def linux_mount_id(fd):
    for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
        if line.startswith("mnt_id:"):
            return line.split()[1]
    raise Refusal("Cannot identify the selected mount")


def linux_removable(device_number):
    device = (Path("/sys/dev/block") / device_number).resolve(strict=True)
    disk = device.parent if (device / "partition").is_file() else device
    return (disk / "removable").read_text().strip() == "1"


def linux_volume(target, fd):
    mount_id = linux_mount_id(fd)
    mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    for line in mounts:
        left, right = line.split(" - ", 1)
        fields = left.split()
        if fields[0] == mount_id:
            break
    else:
        raise Refusal("Cannot identify the selected mount")
    mountpoint = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
    filesystem, source, _ = right.split()
    if (str(target) != mountpoint or fields[3] != "/" or filesystem not in {"vfat", "exfat"}
            or "rw" not in fields[5].split(",") or not source.startswith("/dev/")):
        raise Refusal("Select the root of a writable FAT or exFAT volume")
    if sum(line.split()[2] == fields[2] for line in mounts) != 1:
        raise Refusal("The selected filesystem has multiple mounts")
    if not linux_removable(fields[2]):
        raise Refusal("Linux does not identify this disk as removable")


def diskutil_info(value):
    output = subprocess.run(
        ["/usr/sbin/diskutil", "info", "-plist", str(value)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
    ).stdout
    return plistlib.loads(output)


def mac_volume(target, fd):
    volume = diskutil_info(target)
    node = volume.get("DeviceNode", "")
    if (volume.get("MountPoint") != str(target) or volume.get("WritableVolume") is not True
            or volume.get("FilesystemType") not in {"msdos", "exfat"}
            or not re.fullmatch(r"/dev/disk[0-9]+(?:s[0-9]+)?", node)):
        raise Refusal("Select the root of a writable FAT or exFAT volume")
    info = os.stat(node)
    if not stat.S_ISBLK(info.st_mode) or info.st_rdev != os.fstat(fd).st_dev:
        raise Refusal("Selected mount changed during inspection")
    parent = volume.get("ParentWholeDisk") or volume.get("DeviceIdentifier")
    if not re.fullmatch(r"disk[0-9]+", parent or ""):
        raise Refusal("Cannot identify the whole disk")
    disk = diskutil_info(parent)
    if (disk.get("Internal") is not False or disk.get("RemovableMedia") is not True
            or disk.get("VirtualOrPhysical") != "Physical"):
        raise Refusal("macOS does not identify this disk as physical external removable media")


@contextmanager
def mounted_target(path):
    target = Path(path).absolute()
    if target in {Path("/"), Path("/boot"), Path("/boot/efi"), Path("/boot/firmware")}:
        raise Refusal("Select provisioning media mounted separately from the host boot volume")
    if target != target.resolve(strict=True):
        raise Refusal("Use the canonical mount path, without symlinks")
    fd = os.open(target, DIRECTORY)
    try:
        if sys.platform == "linux":
            linux_volume(target, fd)
        elif sys.platform == "darwin":
            mac_volume(target, fd)
        else:
            raise Refusal("Use a Linux or macOS host")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    finally:
        os.close(fd)


def checked_open(parent, name, flags):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    info = os.fstat(fd)
    try:
        if (info.st_dev != os.fstat(parent).st_dev
                or sys.platform == "linux" and linux_mount_id(fd) != linux_mount_id(parent)
                or not (stat.S_ISDIR(info.st_mode) if flags & os.O_DIRECTORY
                        else stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
            raise Refusal("Unexpected file type or nested mount on provisioning media")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def directory(parent, name, create=False):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = checked_open(parent, name, DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def read_file(parent, name, limit):
    with os.fdopen(checked_open(parent, name, os.O_RDONLY), "rb") as stream:
        return stream.read(limit + 1)


def write_file(parent, name, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(checked_open(parent, name, flags), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def media_id(root, create=False):
    try:
        value = read_file(root, MARKER, 32)
    except FileNotFoundError:
        if not create:
            raise Refusal("No provisioning media ID found") from None
        value = uuid.uuid4().hex.encode()
        if exists(root, MARKER_PENDING):
            read_file(root, MARKER_PENDING, 32)
            os.unlink(MARKER_PENDING, dir_fd=root)
        write_file(root, MARKER_PENDING, value)
        os.rename(MARKER_PENDING, MARKER, src_dir_fd=root, dst_dir_fd=root)
        os.fsync(root)
    if not re.fullmatch(rb"[0-9a-f]{32}", value):
        raise Refusal("Invalid provisioning media ID")
    return value.decode()


def exists(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def scrub_key(folder):
    if not exists(folder, "claim.key"):
        return
    with os.fdopen(checked_open(folder, "claim.key", os.O_RDWR), "r+b") as stream:
        size = os.fstat(stream.fileno()).st_size
        if size > 65:
            raise Refusal("Unexpected claim file size")
        stream.write(b"\0" * size)
        stream.flush()
        os.fsync(stream.fileno())
    os.unlink("claim.key", dir_fd=folder)
    os.fsync(folder)


def serial_folder(container, serial, create=False):
    if set(os.listdir(container)) - {serial}:
        raise Refusal("The medium already contains other enrollment entries")
    return directory(container, serial, create=create)


def verify(root, container, serial, files):
    with directory(root, container) as parent, serial_folder(parent, serial) as folder:
        if set(os.listdir(folder)) != files.keys() or any(
            read_file(folder, name, len(data)) != data for name, data in files.items()
        ):
            raise Refusal("Credential verification failed")


def install(root, serial, files):
    if exists(root, "enrollment"):
        verify(root, "enrollment", serial, files)
        if exists(root, STAGING):
            raise Refusal("Both staged and installed credentials are present")
        os.fsync(root)
        return
    with directory(root, STAGING, create=True) as parent:
        with serial_folder(parent, serial, create=True) as folder:
            if set(os.listdir(folder)) - files.keys():
                raise Refusal("Unexpected files in interrupted staging folder")
            scrub_key(folder)
            for name, data in files.items():
                if exists(folder, name):
                    # Validate the path before removing an interrupted non-key write.
                    read_file(folder, name, len(data))
                    os.unlink(name, dir_fd=folder)
                write_file(folder, name, data)
            os.fsync(folder)
        os.fsync(parent)
    verify(root, STAGING, serial, files)
    os.rename(STAGING, "enrollment", src_dir_fd=root, dst_dir_fd=root)
    os.fsync(root)
    verify(root, "enrollment", serial, files)


@contextmanager
def assignment_log(path, root):
    if os.stat(path.parent).st_dev == os.fstat(root).st_dev:
        raise Refusal("Keep the assignment database off the provisioning volume")
    fd = os.open(path, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid() or info.st_nlink != 1):
            raise Refusal("Use a private assignment database owned by the current user")
    finally:
        os.close(fd)
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute("PRAGMA synchronous = EXTRA")
        db.execute("""CREATE TABLE IF NOT EXISTS assignments (
            fingerprint TEXT PRIMARY KEY, medium TEXT UNIQUE NOT NULL,
            serial TEXT NOT NULL, state TEXT NOT NULL
                CHECK (state IN ('pending', 'written', 'retired'))
        )""")
        yield db


def assign(db, root, archive, entries, requested):
    medium = media_id(root, create=not any(exists(root, p) for p in ("enrollment", STAGING)))
    db.execute("BEGIN IMMEDIATE")
    previous = db.execute(
        "SELECT serial, fingerprint, state FROM assignments WHERE medium = ?", (medium,)
    ).fetchone()
    if previous:
        serial, assigned, state = previous
        if state == "retired" or requested and requested != serial or serial not in entries:
            raise Refusal("This medium already has a different or retired assignment")
        files, fingerprint = read_credentials(archive, entries[serial])
        if fingerprint != assigned:
            raise Refusal("This medium was assigned a different claim")
    else:
        if exists(root, "enrollment") or exists(root, STAGING):
            raise Refusal("Refusing enrollment files without a matching assignment")
        state = "pending"
        for serial in [requested] if requested else sorted(entries, key=str.casefold):
            if serial not in entries:
                raise Refusal("Serial is absent from the archive")
            files, fingerprint = read_credentials(archive, entries[serial])
            duplicate = db.execute(
                "SELECT 1 FROM assignments WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if not duplicate:
                break
            if requested:
                raise Refusal("Claim is already assigned to another medium")
        else:
            raise Refusal("No unassigned claims remain")
        db.execute("INSERT INTO assignments VALUES (?, ?, ?, 'pending')", (fingerprint, medium, serial))
    db.commit()
    if state == "written":
        verify(root, "enrollment", serial, files)
    else:
        install(root, serial, files)
        db.execute("UPDATE assignments SET state = 'written' WHERE medium = ?", (medium,))
    return serial, medium


def scrub(db, root, serial):
    medium = media_id(root)
    changed = db.execute(
        "UPDATE assignments SET state = 'retired' WHERE medium = ? AND serial = ?",
        (medium, serial),
    )
    if not changed.rowcount:
        raise Refusal("The selected serial is not assigned to this medium")
    for container in ("enrollment", STAGING):
        if exists(root, container):
            with directory(root, container) as parent:
                if exists(parent, serial):
                    with serial_folder(parent, serial) as folder:
                        scrub_key(folder)
    os.fsync(root)
    return medium


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True, help="Private shared assignment database")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("write")
    write.add_argument("--archive", type=Path, required=True)
    write.add_argument("--target", required=True, action="append", help="Canonical mounted volume root")
    selection = write.add_mutually_exclusive_group(required=True)
    selection.add_argument("--serial")
    selection.add_argument("--next", action="store_true", help="Use next unassigned serial per target")
    clean = commands.add_parser("scrub")
    clean.add_argument("--target", required=True)
    clean.add_argument("--serial", required=True)
    args = parser.parse_args()
    if args.serial and not SERIAL.fullmatch(args.serial):
        parser.error("Invalid serial")
    if args.command == "write" and (len(set(args.target)) != len(args.target)
                                   or args.serial and len(args.target) != 1):
        parser.error("Repeat --target with distinct volume roots and --next only")
    os.umask(0o077)
    try:
        if args.command == "write":
            with tarfile.open(args.archive, "r:") as archive:
                entries = archive_index(archive)
                for target in args.target:
                    with mounted_target(target) as root, assignment_log(args.log, root) as db:
                        serial, medium = assign(db, root, archive, entries, args.serial)
                        print(f"Written and verified {serial} on {medium}", flush=True)
        else:
            with mounted_target(args.target) as root, assignment_log(args.log, root) as db:
                medium = scrub(db, root, args.serial)
                print(f"Retired {args.serial} on {medium}")
    except (OSError, Refusal, tarfile.TarError, sqlite3.Error, subprocess.SubprocessError,
            plistlib.InvalidFileException) as error:
        # Archive/parser internals can carry input content. Only emit controlled refusals
        # and the OS error description, which excludes path arguments.
        detail = (str(error) if isinstance(error, Refusal) else
                  error.strerror if isinstance(error, OSError) else type(error).__name__)
        parser.exit(1, f"Provisioning stopped: {detail}\n")


if __name__ == "__main__":
    main()
