"""macOS process inspection for the SF-075 launchd tests: libproc through
ctypes, with no third-party dependency. Only runs on macOS."""
import ctypes
import os
import signal
import struct
import sys

LIB = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
LIB.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
LIB.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]

PROC_PIDTBSDINFO = 3
PROC_PIDCOALITIONINFO = 20
BSDINFO_SIZE = 136
SZOMB = 5


def bsdinfo(pid):
    """The status, ppid, pgid and uid of a process, or None."""
    buffer = ctypes.create_string_buffer(BSDINFO_SIZE)
    if LIB.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, buffer, BSDINFO_SIZE) != BSDINFO_SIZE:
        return None
    # pbi_flags, pbi_status, pbi_xstatus, pbi_pid, pbi_ppid, pbi_uid, ...
    fields = struct.unpack_from("<11I", buffer.raw, 0)
    pgid = struct.unpack_from("<I", buffer.raw, 100)[0]
    return dict(status=fields[1], ppid=fields[4], pgid=pgid, uid=fields[5])


def alive(pid):
    info = bsdinfo(pid)
    return info is not None and info["status"] != SZOMB


def coalition(pid):
    """The resource coalition of a process, or None."""
    buffer = ctypes.create_string_buffer(40)
    if LIB.proc_pidinfo(pid, PROC_PIDCOALITIONINFO, 0, buffer, 40) != 40:
        return None
    return struct.unpack_from("<Q", buffer.raw, 0)[0] or None


def pids():
    count = LIB.proc_listallpids(None, 0)
    buffer = (ctypes.c_int * (count + 256))()
    found = LIB.proc_listallpids(buffer, ctypes.sizeof(buffer))
    return [pid for pid in buffer[:found] if pid > 0]


def members(coalition_id):
    """Live processes in a coalition."""
    return sorted(pid for pid in pids() if coalition(pid) == coalition_id and alive(pid))


def escape(target):
    """Try to spawn /bin/sleep into another coalition, the private launchd
    interface (posix_spawnattr_setcoalition_np). Prints the attribute and
    spawn results and the child's coalition; kills the child."""
    attr = ctypes.c_void_p()
    LIB.posix_spawnattr_init(ctypes.byref(attr))
    LIB.posix_spawnattr_setcoalition_np.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_int, ctypes.c_int]
    attribute = LIB.posix_spawnattr_setcoalition_np(ctypes.byref(attr), target, 0, 0)
    pid = ctypes.c_int(0)
    argv = (ctypes.c_char_p * 3)(b"/bin/sleep", b"30", None)
    spawned = LIB.posix_spawn(ctypes.byref(pid), b"/bin/sleep", None, ctypes.byref(attr), argv, None)
    child = coalition(pid.value) if spawned == 0 else None
    if spawned == 0:
        os.kill(pid.value, signal.SIGKILL)
        os.waitpid(pid.value, 0)
    print(f"attribute={attribute} spawn={spawned} child={child} own={coalition(os.getpid())}")


if __name__ == "__main__":
    # `macos_proc.py escape COALITION`: run by a component in the launchd test.
    if len(sys.argv) == 3 and sys.argv[1] == "escape":
        escape(int(sys.argv[2]))
    else:
        raise SystemExit("usage: macos_proc.py escape COALITION")
