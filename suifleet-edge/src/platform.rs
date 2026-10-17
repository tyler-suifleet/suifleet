//! Host process table and boot identity: Linux reads `/proc`, macOS asks the
//! kernel through libproc and sysctl (SF-075). Everything else in the daemon
//! that inspects other processes goes through here.
//!
//! macOS has no cgroups and no child subreaper. Its closest boundary is the
//! *resource coalition*: launchd starts every job in a coalition of its own,
//! each child inherits its parent's at fork, and an unprivileged process
//! cannot leave it (setsid, a cleared environment and reparenting to launchd
//! all keep it). See [`coalition`].

/// One live (non-zombie) process.
pub struct Proc {
    pub pid: u32,
    pub ppid: u32,
    pub pgrp: u32,
}

/// Every live (non-zombie) process this user can inspect.
pub fn processes() -> Vec<Proc> {
    imp::processes()
}

/// Start time of a live (non-zombie) process: clock ticks since boot on
/// Linux, microseconds since the epoch on macOS. Only compared for equality,
/// to tell a process from a later one reusing its PID.
pub fn start_time(pid: u32) -> Option<u64> {
    imp::start_time(pid)
}

/// Whether the environment a process started with holds `entry` (`NAME=value`).
pub fn carries(pid: u32, entry: &[u8]) -> bool {
    imp::environment(pid).is_some_and(|environ| environ.split(|b| *b == 0).any(|var| var == entry))
}

/// An identifier of the current boot: a later boot ended every process.
pub fn boot_id() -> Option<String> {
    imp::boot_id()
}

/// The resource coalition of a process (macOS), when readable.
#[cfg(target_os = "macos")]
pub fn coalition(pid: u32) -> Option<u64> {
    imp::coalition(pid)
}

/// Owner (real UID) of a live process (macOS), when readable.
#[cfg(target_os = "macos")]
pub fn owner(pid: u32) -> Option<u32> {
    imp::bsdinfo(pid).filter(|info| info.pbi_status != libc::SZOMB).map(|info| info.pbi_uid)
}

/// Live members of a coalition (macOS).
#[cfg(target_os = "macos")]
pub fn coalition_members(id: u64) -> Vec<u32> {
    imp::all_pids()
        .into_iter()
        .filter(|&pid| start_time(pid).is_some() && coalition(pid) == Some(id))
        .collect()
}

#[cfg(target_os = "linux")]
mod imp {
    use super::Proc;

    /// Fields after the parenthesised command of /proc/PID/stat: state ppid pgrp ...
    fn stat(pid: u32) -> Option<Vec<String>> {
        let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
        let fields: Vec<String> = stat.rsplit_once(')')?.1.split_whitespace().map(str::to_owned).collect();
        (fields.first().map(String::as_str) != Some("Z")).then_some(fields)
    }

    pub fn processes() -> Vec<Proc> {
        let Ok(entries) = std::fs::read_dir("/proc") else {
            return Vec::new();
        };
        entries
            .flatten()
            .filter_map(|entry| {
                let pid = entry.file_name().to_str()?.parse::<u32>().ok()?;
                let fields = stat(pid)?;
                Some(Proc { pid, ppid: fields.get(1)?.parse().ok()?, pgrp: fields.get(2)?.parse().ok()? })
            })
            .collect()
    }

    pub fn start_time(pid: u32) -> Option<u64> {
        // Fields 3.. follow the command; starttime is field 22.
        stat(pid)?.get(19)?.parse().ok()
    }

    pub fn environment(pid: u32) -> Option<Vec<u8>> {
        std::fs::read(format!("/proc/{pid}/environ")).ok()
    }

    pub fn boot_id() -> Option<String> {
        std::fs::read_to_string("/proc/sys/kernel/random/boot_id").ok().map(|id| id.trim().to_owned())
    }
}

#[cfg(target_os = "macos")]
mod imp {
    use super::Proc;

    /// `PROC_PIDCOALITIONINFO` of <sys/proc_info.h>: a process's coalition
    /// IDs, indexed by coalition type (resource, jetsam).
    const PROC_PIDCOALITIONINFO: libc::c_int = 20;
    const COALITION_TYPE_RESOURCE: usize = 0;
    #[repr(C)]
    #[derive(Default)]
    struct CoalitionInfo {
        coalition_id: [u64; 2],
        reserved: [u64; 3],
    }

    pub fn bsdinfo(pid: u32) -> Option<libc::proc_bsdinfo> {
        // SAFETY: proc_bsdinfo is plain data; the kernel fills at most `size` bytes.
        let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
        let size = std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int;
        let written = unsafe {
            libc::proc_pidinfo(pid as libc::c_int, libc::PROC_PIDTBSDINFO, 0, &mut info as *mut _ as *mut libc::c_void, size)
        };
        (written == size).then_some(info)
    }

    pub fn all_pids() -> Vec<u32> {
        // The table can grow between the two calls: leave headroom and retry.
        for _ in 0..4 {
            // SAFETY: a null buffer asks for the number of processes.
            let count = unsafe { libc::proc_listallpids(std::ptr::null_mut(), 0) };
            if count <= 0 {
                return Vec::new();
            }
            let mut pids = vec![0 as libc::c_int; count as usize + 64];
            let bytes = (pids.len() * std::mem::size_of::<libc::c_int>()) as libc::c_int;
            // SAFETY: the buffer holds `bytes` bytes.
            let found = unsafe { libc::proc_listallpids(pids.as_mut_ptr() as *mut libc::c_void, bytes) };
            if found < 0 {
                return Vec::new();
            }
            if (found as usize) < pids.len() {
                pids.truncate(found as usize);
                return pids.into_iter().filter(|&pid| pid > 0).map(|pid| pid as u32).collect();
            }
        }
        Vec::new()
    }

    pub fn processes() -> Vec<Proc> {
        all_pids()
            .into_iter()
            .filter_map(|pid| {
                let info = bsdinfo(pid)?;
                (info.pbi_status != libc::SZOMB).then_some(Proc { pid, ppid: info.pbi_ppid, pgrp: info.pbi_pgid })
            })
            .collect()
    }

    pub fn start_time(pid: u32) -> Option<u64> {
        let info = bsdinfo(pid)?;
        (info.pbi_status != libc::SZOMB).then(|| info.pbi_start_tvsec * 1_000_000 + info.pbi_start_tvusec)
    }

    /// KERN_PROCARGS2: argc, the executable path, argv and then the
    /// environment the process was started with, NUL separated.
    pub fn environment(pid: u32) -> Option<Vec<u8>> {
        let mut size: libc::c_int = 0;
        let mut length = std::mem::size_of::<libc::c_int>();
        let mut mib = [libc::CTL_KERN, libc::KERN_ARGMAX];
        // SAFETY: reads one int into `size`.
        if unsafe { libc::sysctl(mib.as_mut_ptr(), 2, &mut size as *mut _ as *mut libc::c_void, &mut length, std::ptr::null_mut(), 0) } != 0 {
            return None;
        }
        let mut buffer = vec![0u8; size.max(0) as usize];
        let mut length = buffer.len();
        let mut mib = [libc::CTL_KERN, libc::KERN_PROCARGS2, pid as libc::c_int];
        // SAFETY: the kernel writes at most `length` bytes into the buffer.
        if unsafe { libc::sysctl(mib.as_mut_ptr(), 3, buffer.as_mut_ptr() as *mut libc::c_void, &mut length, std::ptr::null_mut(), 0) } != 0 {
            return None;
        }
        buffer.truncate(length);
        let argc = i32::from_ne_bytes(buffer.get(..4)?.try_into().ok()?).max(0) as usize;
        let mut rest = &buffer[4..];
        // The executable path, then padding NULs.
        let end = rest.iter().position(|b| *b == 0)?;
        rest = &rest[end..];
        let start = rest.iter().position(|b| *b != 0)?;
        rest = &rest[start..];
        for _ in 0..argc {
            let end = rest.iter().position(|b| *b == 0)?;
            rest = &rest[(end + 1).min(rest.len())..];
        }
        Some(rest.to_vec())
    }

    pub fn boot_id() -> Option<String> {
        let mut buffer = [0u8; 64];
        let mut length = buffer.len();
        // SAFETY: a NUL-terminated name and a buffer of `length` bytes.
        let result = unsafe {
            libc::sysctlbyname(c"kern.bootsessionuuid".as_ptr(), buffer.as_mut_ptr() as *mut libc::c_void, &mut length, std::ptr::null_mut(), 0)
        };
        if result != 0 {
            return None;
        }
        let text = std::str::from_utf8(&buffer[..length]).ok()?.trim_end_matches('\0').trim();
        (!text.is_empty()).then(|| text.to_owned())
    }

    pub fn coalition(pid: u32) -> Option<u64> {
        let mut info = CoalitionInfo::default();
        let size = std::mem::size_of::<CoalitionInfo>() as libc::c_int;
        // SAFETY: CoalitionInfo matches struct proc_pidcoalitioninfo.
        let written = unsafe {
            libc::proc_pidinfo(pid as libc::c_int, PROC_PIDCOALITIONINFO, 0, &mut info as *mut _ as *mut libc::c_void, size)
        };
        let id = info.coalition_id[COALITION_TYPE_RESOURCE];
        (written == size && id != 0).then_some(id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sees_this_process_and_a_child() {
        let me = std::process::id();
        assert!(start_time(me).is_some());
        assert!(boot_id().is_some_and(|id| !id.is_empty()));
        let mut child = std::process::Command::new("sleep")
            .arg("30")
            .env("SUIFLEET_PLATFORM_TEST", "marker")
            .spawn()
            .unwrap();
        let pid = child.id();
        let table = processes();
        let entry = table.iter().find(|p| p.pid == pid).expect("child listed");
        assert_eq!(entry.ppid, me);
        // A short-lived process may not have exec'd yet: wait for its environment.
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
        while !carries(pid, b"SUIFLEET_PLATFORM_TEST=marker") {
            assert!(std::time::Instant::now() < deadline, "child environment not found");
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        assert!(!carries(pid, b"SUIFLEET_PLATFORM_TEST=other"));
        let started = start_time(pid).unwrap();
        child.kill().unwrap();
        child.wait().unwrap();
        assert_ne!(start_time(pid), Some(started), "a reaped process still has a start time");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn children_inherit_the_coalition() {
        let mut child = std::process::Command::new("sleep").arg("30").spawn().unwrap();
        let mine = coalition(std::process::id()).expect("own coalition readable");
        assert_eq!(coalition(child.id()), Some(mine));
        assert!(coalition_members(mine).contains(&child.id()));
        assert_eq!(owner(child.id()), Some(unsafe { libc::getuid() }));
        child.kill().unwrap();
        child.wait().unwrap();
    }
}
