"""Offline tests for the host enrollment media writer."""

from contextlib import ExitStack, contextmanager, redirect_stdout
import errno
import io
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import provision_enrollment_media as media


def add(archive, path, data):
    member = tarfile.TarInfo(path)
    if data is None:
        member.type = tarfile.DIRTYPE
    else:
        member.size = len(data)
    archive.addfile(member, None if data is None else io.BytesIO(data))


def make_archive(path, extra=None):
    with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as archive:
        add(archive, "enrollment/", None)
        for serial, digit in (("unit-01", b"1"), ("unit-02", b"2"), ("unit-03", b"1")):
            prefix = f"enrollment/{serial}/"
            add(archive, prefix, None)
            add(archive, prefix + "claim.key", digit * 64 + b"\n")
            add(archive, prefix + "enroll.toml",
                b'claim_key_path = "claim.key"\nmanifest_path = "deployment.json"\n')
            add(archive, prefix + "deployment.json", b"{}\n")
        if extra:
            add(archive, *extra)


class ArchiveTests(unittest.TestCase):
    def test_archive_shape_and_case_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.tar"
            for extra in (("enrollment/../claim.key", b"bad"),
                          ("enrollment/UNIT-01/claim.key", b"bad"),
                          ("enrollment/unit-01/extra", b"bad")):
                with self.subTest(extra=extra):
                    make_archive(path, extra)
                    with tarfile.open(path) as archive, self.assertRaises(media.Refusal):
                        media.archive_index(archive)

    def test_mac_requires_physical_removable_disk_and_matching_volume(self):
        volume = {
            "DeviceNode": "/dev/disk4s1", "MountPoint": "/Volumes/BOOT",
            "WritableVolume": True, "FilesystemType": "msdos", "ParentWholeDisk": "disk4",
        }
        disk = {"Internal": False, "RemovableMedia": True, "VirtualOrPhysical": "Physical"}
        block = SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=123)
        for disk_info, device_number, accepted in (
            (disk, 123, True),
            (disk, 456, False),
            ({**disk, "Internal": True}, 123, False),
            ({**disk, "RemovableMedia": False}, 123, False),
            ({**disk, "VirtualOrPhysical": "Virtual"}, 123, False),
        ):
            with (patch.object(media, "diskutil_info", side_effect=[volume, disk_info]),
                  patch.object(media.os, "stat", return_value=block),
                  patch.object(media.os, "fstat", return_value=SimpleNamespace(st_dev=device_number))):
                if accepted:
                    media.mac_volume(Path("/Volumes/BOOT"), 42)
                else:
                    with self.assertRaises(media.Refusal):
                        media.mac_volume(Path("/Volumes/BOOT"), 42)

    @unittest.skipUnless(sys.platform == "linux", "Linux mount metadata")
    def test_ordinary_directory_and_symlink_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "ordinary"
            folder.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(folder)
            for path in (folder, link, Path("/boot")):
                with self.subTest(path=path), self.assertRaises(media.Refusal):
                    with media.mounted_target(path):
                        self.fail("unexpected target")
            self.assertEqual(list(folder.iterdir()), [])


@contextmanager
def fat_image(work, name):
    image = work / f"{name}.img"
    target = work / f"{name} volume"
    target.mkdir()
    with image.open("wb") as stream:
        stream.truncate(32 * 1024 * 1024)
    subprocess.run(["mkfs.fat", str(image)], check=True, stdout=subprocess.DEVNULL)
    loop = subprocess.check_output(["losetup", "--find", "--show", str(image)], text=True).strip()
    try:
        subprocess.run(["mount", "-t", "vfat", loop, str(target)], check=True)
        try:
            yield target, loop
        finally:
            subprocess.run(["umount", str(target)], check=True)
    finally:
        subprocess.run(["losetup", "-d", loop], check=True)


@unittest.skipUnless(os.environ.get("SUIFLEET_TEST_FAT_LOOP") == "1",
                     "set SUIFLEET_TEST_FAT_LOOP=1 with Linux loop/mount privileges")
class FatImageTests(unittest.TestCase):
    def test_assignment_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            work = Path(tmp).resolve()
            first, first_loop = stack.enter_context(fat_image(work, "first"))
            second, _ = stack.enter_context(fat_image(work, "second"))
            log = work / "assignments.db"
            archive_path = work / "credentials.tar"
            make_archive(archive_path)
            with self.assertRaisesRegex(media.Refusal, "removable"):
                with media.mounted_target(first):
                    self.fail("real loop device must be refused")
            self.assertEqual(list(first.iterdir()), [])
            alias = work / "alias"
            alias.mkdir()
            subprocess.run(["mount", "--bind", str(first), str(alias)], check=True)
            try:
                with self.assertRaisesRegex(media.Refusal, "multiple mounts"):
                    with media.mounted_target(first):
                        self.fail("an ambiguously mounted filesystem must be refused")
            finally:
                subprocess.run(["umount", str(alias)], check=True)
            self.assertFalse(log.exists())
            archive = stack.enter_context(tarfile.open(archive_path))
            entries = media.archive_index(archive)

            def assign(target, serial=None):
                # Change only the removable bit. Actual mount IDs, FAT operations,
                # directory locking, permissions and filesystem syncs are exercised.
                with (patch.object(media, "linux_removable", return_value=True),
                      media.mounted_target(target) as root, media.assignment_log(log, root) as db):
                    return media.assign(db, root, archive, entries, serial)

            (first / media.MARKER_PENDING).write_bytes(b"interrupted")
            real_write = media.write_file

            def interrupt(parent, name, data):
                if name == "claim.key":
                    real_write(parent, name, data[:17])
                    raise OSError(errno.EIO, "simulated interruption")
                real_write(parent, name, data)

            with patch.object(media, "write_file", side_effect=interrupt):
                with self.assertRaisesRegex(OSError, "simulated interruption"):
                    assign(first, "unit-01")
            self.assertFalse((first / "enrollment").exists())
            self.assertTrue((first / media.STAGING / "unit-01" / "claim.key").exists())
            for serial in ("unit-01", "unit-03"):
                with self.assertRaisesRegex(media.Refusal, "already assigned"):
                    assign(second, serial)
            self.assertFalse((second / "enrollment").exists())

            real_verify = media.verify

            def interrupt_after_rename(root, container, serial, files):
                if container == "enrollment":
                    raise OSError(errno.EIO, "simulated post-rename interruption")
                real_verify(root, container, serial, files)

            with patch.object(media, "verify", side_effect=interrupt_after_rename):
                with self.assertRaisesRegex(OSError, "simulated post-rename interruption"):
                    assign(first, "unit-01")
            self.assertEqual((first / "enrollment/unit-01/claim.key").read_bytes(), b"1" * 64 + b"\n")
            self.assertFalse((first / media.STAGING).exists())
            serial, first_id = assign(first)
            self.assertEqual(serial, "unit-01")
            self.assertEqual(assign(first, serial), (serial, first_id))
            output = io.StringIO()
            command = ["writer", "--log", str(log), "write", "--archive", str(archive_path),
                       "--next", "--target", str(first), "--target", str(second)]
            with (patch.object(media, "linux_removable", return_value=True),
                  patch.object(sys, "argv", command), redirect_stdout(output)):
                media.main()
            self.assertEqual(output.getvalue().splitlines()[0],
                             f"Written and verified unit-01 on {first_id}")
            serial, second_id = assign(second)
            self.assertEqual(output.getvalue().splitlines()[1],
                             f"Written and verified unit-02 on {second_id}")
            self.assertEqual(serial, "unit-02")
            self.assertNotEqual(first_id, second_id)
            self.assertEqual(set(p.name for p in (second / "enrollment").iterdir()), {serial})

            # Read again after a real unmount/remount.
            subprocess.run(["umount", str(first)], check=True)
            subprocess.run(["mount", "-t", "vfat", first_loop, str(first)], check=True)
            self.assertEqual(assign(first), ("unit-01", first_id))
            with (patch.object(media, "linux_removable", return_value=True),
                  media.mounted_target(first) as root, media.assignment_log(log, root) as db):
                with self.assertRaisesRegex(media.Refusal, "not assigned"):
                    media.scrub(db, root, "unit-02")
                self.assertEqual(media.scrub(db, root, "unit-01"), first_id)
                self.assertEqual(media.scrub(db, root, "unit-01"), first_id)
            self.assertFalse((first / "enrollment/unit-01/claim.key").exists())
            self.assertTrue((first / "enrollment/unit-01/deployment.json").exists())
            with self.assertRaisesRegex(media.Refusal, "retired"):
                assign(first, "unit-01")
            with sqlite3.connect(log) as db:
                self.assertEqual(db.execute(
                    "SELECT serial, medium, state FROM assignments ORDER BY serial"
                ).fetchall(), [("unit-01", first_id, "retired"), ("unit-02", second_id, "written")])
            self.assertNotIn(b"1" * 64, log.read_bytes())
            self.assertNotIn(b"2" * 64, log.read_bytes())


if __name__ == "__main__":
    unittest.main()
