use crate::{quote, Manifest, Request};
use anyhow::{bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    ffi::c_void,
    fs,
    io::Write,
    mem::{size_of, zeroed},
    os::windows::ffi::OsStrExt,
    path::{Path, PathBuf},
    ptr::{null, null_mut},
    sync::atomic::{AtomicBool, Ordering},
    thread,
    time::{Duration, Instant},
};
use windows_sys::Win32::{
    Foundation::*,
    Security::{Authorization::*, *},
    Storage::FileSystem::*,
    System::{JobObjects::*, Pipes::*, Services::*, Threading::*},
};

static STOP: AtomicBool = AtomicBool::new(false);
const SERVICE: &str = "SuiFleet";
fn wide(s: impl AsRef<std::ffi::OsStr>) -> Vec<u16> {
    s.as_ref().encode_wide().chain(Some(0)).collect()
}
fn check(ok: i32) -> Result<()> {
    if ok == 0 {
        Err(std::io::Error::last_os_error().into())
    } else {
        Ok(())
    }
}
struct Handle(HANDLE);
impl Handle {
    fn new(h: HANDLE) -> Result<Self> {
        ensure!(
            !h.is_null() && h != INVALID_HANDLE_VALUE,
            "Win32 handle: {}",
            std::io::Error::last_os_error()
        );
        Ok(Self(h))
    }
}
impl Drop for Handle {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}
// Handles remain on the service thread; none are inherited by component processes.
struct Descriptor(PSECURITY_DESCRIPTOR);
impl Descriptor {
    fn new(sddl: &str) -> Result<Self> {
        let mut ptr = null_mut();
        unsafe {
            check(ConvertStringSecurityDescriptorToSecurityDescriptorW(
                wide(sddl).as_ptr(),
                SDDL_REVISION_1,
                &mut ptr,
                null_mut(),
            ))?;
        }
        Ok(Self(ptr))
    }
    fn attrs(&self) -> SECURITY_ATTRIBUTES {
        SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: self.0,
            bInheritHandle: 0,
        }
    }
}
impl Drop for Descriptor {
    fn drop(&mut self) {
        unsafe {
            LocalFree(self.0);
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    root: PathBuf,
    pipe: String,
    operator_sid: String,
    component_sid: String,
    component_user: String,
    component_password: String,
}
impl Config {
    fn load(path: &Path) -> Result<Self> {
        let cfg: Self = serde_json::from_slice(&fs::read(path)?)?;
        ensure!(
            cfg.root.is_absolute() && path.is_absolute(),
            "absolute paths required"
        );
        ensure!(
            cfg.pipe.starts_with("suifleet-")
                && cfg
                    .pipe
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'-'),
            "invalid pipe name"
        );
        for sid in [&cfg.operator_sid, &cfg.component_sid] {
            ensure!(
                sid.starts_with("S-1-")
                    && sid
                        .bytes()
                        .all(|b| b == b'S' || b == b'-' || b.is_ascii_digit()),
                "invalid SID"
            );
        }
        ensure!(
            cfg.operator_sid != cfg.component_sid && cfg.component_sid != "S-1-5-18",
            "component requires separate unprivileged account"
        );
        ensure!(
            !cfg.component_user.contains('\0') && !cfg.component_password.contains('\0'),
            "invalid logon credentials"
        );
        // Config and runtime root must be provisioned by the elevated installer.
        reject_reparse(&cfg.root)?;
        ensure!(
            path.parent() == Some(cfg.root.as_path()),
            "config must be directly within protected root"
        );
        protect(
            &cfg.root,
            &format!(
                "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{})(A;;0x20;;;{})",
                cfg.operator_sid, cfg.component_sid
            ),
        )?;
        protect(
            path,
            &format!("D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;{})", cfg.operator_sid),
        )?;
        Ok(cfg)
    }
    fn pipe_path(&self) -> String {
        format!(r"\\.\pipe\{}", self.pipe)
    }
}
fn reject_reparse(path: &Path) -> Result<()> {
    let mut current = Some(path);
    while let Some(p) = current {
        if p.as_os_str().is_empty() {
            break;
        }
        let attr = unsafe { GetFileAttributesW(wide(p).as_ptr()) };
        ensure!(
            attr != INVALID_FILE_ATTRIBUTES && attr & FILE_ATTRIBUTE_REPARSE_POINT == 0,
            "missing or reparse runtime path"
        );
        current = p.parent();
    }
    Ok(())
}
fn protect(path: &Path, sddl: &str) -> Result<()> {
    reject_reparse(path)?;
    let sd = Descriptor::new(sddl)?;
    unsafe {
        check(SetFileSecurityW(
            wide(path).as_ptr(),
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            sd.0,
        ))
    }
}
fn token_sid(token: HANDLE) -> Result<String> {
    let mut len = 0;
    unsafe {
        GetTokenInformation(token, TokenUser, null_mut(), 0, &mut len);
    }
    ensure!(len > 0 && len < 65536, "invalid token size");
    // u64 backing storage ensures TOKEN_USER alignment.
    let mut data = vec![0u64; (len as usize).div_ceil(8)];
    unsafe {
        check(GetTokenInformation(
            token,
            TokenUser,
            data.as_mut_ptr().cast(),
            len,
            &mut len,
        ))?;
        let user = &*(data.as_ptr().cast::<TOKEN_USER>());
        let mut text = null_mut();
        check(ConvertSidToStringSidW(user.User.Sid, &mut text))?;
        let mut n = 0;
        while *text.add(n) != 0 {
            n += 1;
        }
        let sid = String::from_utf16_lossy(std::slice::from_raw_parts(text, n));
        LocalFree(text.cast());
        Ok(sid)
    }
}
fn component_token(cfg: &Config) -> Result<Handle> {
    let mut token = null_mut();
    let mut password = wide(&cfg.component_password);
    let result = unsafe {
        LogonUserW(
            wide(&cfg.component_user).as_ptr(),
            wide(".").as_ptr(),
            password.as_ptr(),
            LOGON32_LOGON_INTERACTIVE,
            LOGON32_PROVIDER_DEFAULT,
            &mut token,
        )
    };
    password.fill(0);
    check(result).context("component logon")?;
    let token = Handle::new(token)?;
    ensure!(
        token_sid(token.0)? == cfg.component_sid,
        "component account SID mismatch"
    );
    // Reject elevated/admin principals, even if accidentally configured as a component.
    let mut elevation: TOKEN_ELEVATION = unsafe { zeroed() };
    let mut len = 0;
    unsafe {
        check(GetTokenInformation(
            token.0,
            TokenElevation,
            (&mut elevation as *mut TOKEN_ELEVATION).cast(),
            size_of::<TOKEN_ELEVATION>() as u32,
            &mut len,
        ))?;
    }
    ensure!(
        elevation.TokenIsElevated == 0,
        "component token must not be elevated"
    );
    Ok(token)
}

struct Process {
    process: Handle,
    job: Handle,
}
impl Process {
    fn spawn(token: HANDLE, exe: &Path, args: &[String], work: &Path) -> Result<Self> {
        let job = Handle::new(unsafe { CreateJobObjectW(null(), null()) })?;
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { zeroed() };
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        unsafe {
            check(SetInformationJobObject(
                job.0,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                size_of_val(&limits) as u32,
            ))?;
        }
        let mut startup: STARTUPINFOW = unsafe { zeroed() };
        startup.cb = size_of_val(&startup) as u32;
        let mut info: PROCESS_INFORMATION = unsafe { zeroed() };
        let mut cmd = wide(
            std::iter::once(quote(&exe.to_string_lossy()))
                .chain(args.iter().map(|a| quote(a)))
                .collect::<Vec<_>>()
                .join(" "),
        );
        // Deliberately do not inherit daemon environment, credentials, or handles.
        let system = std::env::var("SystemRoot").context("SystemRoot missing")?;
        let mut env: Vec<u16> = format!(
            "SystemRoot={system}\0TEMP={}\0TMP={}\0\0",
            work.display(),
            work.display()
        )
        .encode_utf16()
        .collect();
        unsafe {
            check(CreateProcessAsUserW(
                token,
                wide(exe).as_ptr(),
                cmd.as_mut_ptr(),
                null(),
                null(),
                0,
                CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
                env.as_mut_ptr().cast(),
                wide(work).as_ptr(),
                &startup,
                &mut info,
            ))?;
        }
        let process = Handle::new(info.hProcess)?;
        let thread = Handle::new(info.hThread)?;
        if let Err(e) = unsafe { check(AssignProcessToJobObject(job.0, process.0)) } {
            unsafe {
                TerminateProcess(process.0, 1);
                WaitForSingleObject(process.0, 5000);
            }
            return Err(e.context("job assignment; child never resumed"));
        }
        let child = Self { process, job };
        ensure!(
            unsafe { ResumeThread(thread.0) } != u32::MAX,
            "cannot resume component"
        );
        Ok(child)
    }
    fn exited(&self) -> bool {
        unsafe { WaitForSingleObject(self.process.0, 0) == WAIT_OBJECT_0 }
    }
    fn wait(&self, timeout: Duration) -> Result<()> {
        let deadline = Instant::now() + timeout;
        while !self.exited() {
            ensure!(!STOP.load(Ordering::SeqCst), "service stopping");
            ensure!(Instant::now() < deadline, "lifecycle timeout");
            thread::sleep(Duration::from_millis(25));
        }
        let mut code = 0;
        unsafe {
            check(GetExitCodeProcess(self.process.0, &mut code))?;
        }
        ensure!(code == 0, "lifecycle exit {code}");
        Ok(())
    }
}
impl Drop for Process {
    fn drop(&mut self) {
        unsafe {
            TerminateJobObject(self.job.0, 1);
            WaitForSingleObject(self.process.0, 5000);
        }
    }
}

#[derive(Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct State {
    manifest: Option<Manifest>,
    phase: String,
    error: Option<String>,
}
fn save(root: &Path, state: &State) -> Result<()> {
    let temp = root.join("state.tmp");
    let target = root.join("state.json");
    let mut file = fs::File::create(&temp)?;
    file.write_all(&serde_json::to_vec(state)?)?;
    file.sync_all()?;
    drop(file);
    unsafe {
        check(MoveFileExW(
            wide(&temp).as_ptr(),
            wide(&target).as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        ))
    }
}
struct Runtime {
    cfg: Config,
    token: Handle,
    state: State,
    child: Option<Process>,
    restarts: u32,
    next_restart: Instant,
}
impl Runtime {
    fn open(cfg: Config) -> Result<Self> {
        let token = component_token(&cfg)?;
        let mut state: State = match fs::read(cfg.root.join("state.json")) {
            Ok(bytes) => serde_json::from_slice(&bytes)?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => State::default(),
            Err(e) => return Err(e.into()),
        };
        if state.phase == "installing" {
            state.phase = "interrupted".into();
            state.error = Some(
                "Interrupted install; explicit operator reinstall required; install not replayed"
                    .into(),
            );
            save(&cfg.root, &state)?;
        }
        let mut runtime = Self {
            cfg,
            token,
            state,
            child: None,
            restarts: 0,
            next_restart: Instant::now(),
        };
        if runtime.state.phase == "running" {
            runtime.start()?;
        }
        Ok(runtime)
    }
    fn paths(&self, m: &Manifest) -> (PathBuf, PathBuf) {
        let dir = self.cfg.root.join("components").join(&m.sha256);
        (dir.join("artifact.exe"), dir.join("work"))
    }
    fn start(&mut self) -> Result<()> {
        ensure!(self.child.is_none(), "component already running");
        ensure!(
            self.state.phase != "interrupted"
                && self.state.phase != "installing"
                && self.state.phase != "failed",
            "explicit reinstall required after ambiguous or failed lifecycle"
        );
        let m = self
            .state
            .manifest
            .as_ref()
            .context("no installed component")?;
        m.validate()?;
        let (exe, work) = self.paths(m);
        reject_reparse(&exe)?;
        reject_reparse(&work)?;
        ensure!(
            hex::encode(Sha256::digest(fs::read(&exe)?)) == m.sha256,
            "installed artifact digest mismatch"
        );
        self.child = Some(Process::spawn(self.token.0, &exe, &m.run, &work)?);
        self.state.phase = "running".into();
        self.state.error = None;
        save(&self.cfg.root, &self.state)
    }
    fn stop(&mut self, shutdown: bool) -> Result<()> {
        if self.child.is_some() && shutdown {
            if let Some(m) = &self.state.manifest {
                if let Some(args) = &m.shutdown {
                    let (exe, work) = self.paths(m);
                    // Failure still closes the supervised job; never leave descendants behind.
                    let result = Process::spawn(self.token.0, &exe, args, &work)
                        .and_then(|p| p.wait(Duration::from_secs(m.timeout_seconds.into())));
                    if let Err(e) = result {
                        self.state.error = Some(format!("shutdown failed: {e:#}"));
                    }
                }
            }
        }
        self.child.take();
        self.state.phase = "stopped".into();
        save(&self.cfg.root, &self.state)
    }
    fn install(&mut self, m: Manifest, source: &Path) -> Result<()> {
        m.validate()?;
        // Operator-authenticated local source only; never interpret component paths as authority.
        ensure!(
            source.is_absolute() && !source.to_string_lossy().starts_with(r"\\"),
            "local absolute artifact path required"
        );
        reject_reparse(source)?;
        ensure!(
            fs::metadata(source)?.len() <= 128 * 1024 * 1024,
            "artifact too large"
        );
        let bytes = fs::read(source)?;
        ensure!(bytes.len() <= 128 * 1024 * 1024, "artifact too large");
        ensure!(
            hex::encode(Sha256::digest(&bytes)) == m.sha256,
            "artifact digest mismatch"
        );
        self.stop(true)?;
        let (exe, work) = self.paths(&m);
        fs::create_dir_all(&work)?;
        reject_reparse(&work)?;
        protect(
            exe.parent().unwrap(),
            &format!(
                "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{})(A;OICI;FRFX;;;{})",
                self.cfg.operator_sid, self.cfg.component_sid
            ),
        )?;
        if !exe.exists() {
            let mut f = fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&exe)?;
            f.write_all(&bytes)?;
            f.sync_all()?;
        }
        ensure!(
            hex::encode(Sha256::digest(fs::read(&exe)?)) == m.sha256,
            "cached digest mismatch"
        );
        protect(
            &work,
            &format!(
                "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{})(A;OICI;0x1301bf;;;{})",
                self.cfg.operator_sid, self.cfg.component_sid
            ),
        )?;
        self.state = State {
            manifest: Some(m.clone()),
            phase: "installing".into(),
            error: None,
        };
        save(&self.cfg.root, &self.state)?;
        if let Some(args) = &m.install {
            if let Err(e) = Process::spawn(self.token.0, &exe, args, &work)
                .and_then(|p| p.wait(Duration::from_secs(m.timeout_seconds.into())))
            {
                self.state.phase = "failed".into();
                self.state.error = Some(format!("install failed: {e:#}"));
                save(&self.cfg.root, &self.state)?;
                return Err(e);
            }
        }
        self.state.phase = "installed".into();
        save(&self.cfg.root, &self.state)?;
        self.restarts = 0;
        self.start()
    }
    fn poll(&mut self) -> Result<()> {
        if self.child.as_ref().is_some_and(Process::exited) {
            self.child.take();
            self.next_restart = Instant::now() + Duration::from_secs(1);
            self.state.error = Some("component exited; descendants terminated".into());
            save(&self.cfg.root, &self.state)?;
        }
        if self.child.is_none()
            && self.state.phase == "running"
            && Instant::now() >= self.next_restart
        {
            let max = self
                .state
                .manifest
                .as_ref()
                .context("missing manifest")?
                .max_restarts;
            if self.restarts >= max {
                self.state.phase = "failed".into();
                save(&self.cfg.root, &self.state)?;
            } else {
                self.restarts += 1;
                self.start()?;
            }
        }
        Ok(())
    }
    fn request(&mut self, sid: &str, request: Request) -> Result<serde_json::Value> {
        let operator = sid == self.cfg.operator_sid || sid == "S-1-5-18";
        ensure!(operator || sid == self.cfg.component_sid, "caller denied");
        if !matches!(request, Request::Status { .. }) {
            ensure!(operator, "operator permission required");
        }
        match request {
            Request::Status {} => {}
            Request::Install { manifest, artifact } => {
                self.install(manifest, Path::new(&artifact))?
            }
            Request::Stop {} => self.stop(true)?,
            Request::Start {} => {
                self.restarts = 0;
                self.start()?;
            }
        }
        Ok(
            serde_json::json!({"phase": self.state.phase, "error": self.state.error, "name": self.state.manifest.as_ref().map(|m| &m.name), "version": self.state.manifest.as_ref().map(|m| &m.version), "running": self.child.is_some(), "restarts": self.restarts}),
        )
    }
}

fn caller(pipe: HANDLE) -> Result<String> {
    unsafe {
        check(ImpersonateNamedPipeClient(pipe))?;
    }
    struct Revert;
    impl Drop for Revert {
        fn drop(&mut self) {
            if unsafe { RevertToSelf() } == 0 {
                std::process::abort();
            }
        }
    }
    let _revert = Revert;
    let mut token = null_mut();
    unsafe {
        check(OpenThreadToken(
            GetCurrentThread(),
            TOKEN_QUERY,
            1,
            &mut token,
        ))?;
    }
    token_sid(Handle::new(token)?.0)
}
struct Pipe {
    handle: Handle,
    connected: bool,
    replied: bool,
    bytes: Vec<u8>,
    deadline: Instant,
}
impl Pipe {
    fn bind(cfg: &Config) -> Result<Self> {
        // Specific file rights omit FILE_CREATE_PIPE_INSTANCE (0x4). Reject all remote clients.
        let sd = Descriptor::new(&format!(
            "D:P(A;;FA;;;SY)(A;;0x12019b;;;{})(A;;0x12019b;;;{})",
            cfg.operator_sid, cfg.component_sid
        ))?;
        let sa = sd.attrs();
        let handle = Handle::new(unsafe {
            CreateNamedPipeW(
                wide(cfg.pipe_path()).as_ptr(),
                PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_NOWAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1,
                65536,
                65536,
                1000,
                &sa,
            )
        })?;
        Ok(Self {
            handle,
            connected: false,
            replied: false,
            bytes: Vec::new(),
            deadline: Instant::now(),
        })
    }
    fn reset(&mut self) {
        unsafe {
            DisconnectNamedPipe(self.handle.0);
        }
        self.connected = false;
        self.replied = false;
        self.bytes.clear();
    }
    fn poll(&mut self, runtime: &mut Runtime) -> Result<()> {
        if !self.connected {
            let ok = unsafe { ConnectNamedPipe(self.handle.0, null_mut()) };
            let error = unsafe { GetLastError() };
            if ok == 0 && error != ERROR_PIPE_CONNECTED {
                ensure!(
                    error == ERROR_PIPE_LISTENING || error == ERROR_NO_DATA,
                    "pipe connect failed: {error}"
                );
                return Ok(());
            }
            self.connected = true;
            self.deadline = Instant::now() + Duration::from_secs(2);
        }
        let mut buf = [0u8; 4096];
        let mut read = 0;
        let ok = unsafe {
            ReadFile(
                self.handle.0,
                buf.as_mut_ptr(),
                buf.len() as u32,
                &mut read,
                null_mut(),
            )
        };
        if ok == 0 {
            let error = unsafe { GetLastError() };
            if error == ERROR_BROKEN_PIPE || Instant::now() > self.deadline {
                self.reset();
                return Ok(());
            }
            if error == ERROR_NO_DATA {
                return Ok(());
            }
            self.reset();
            return Ok(());
        }
        if self.replied {
            self.reset();
            return Ok(());
        }
        self.bytes.extend_from_slice(&buf[..read as usize]);
        if self.bytes.len() > 65536 || Instant::now() > self.deadline {
            self.reset();
            return Ok(());
        }
        if self.bytes.last() != Some(&b'\n') {
            return Ok(());
        }
        // Identity is obtained from the message just read; supplied names/SIDs are never trusted.
        let result = caller(self.handle.0).and_then(|sid| {
            let request = serde_json::from_slice(&self.bytes)?;
            runtime.request(&sid, request)
        });
        let value = match result {
            Ok(value) => serde_json::json!({"ok": true, "status": value}),
            Err(e) => serde_json::json!({"ok": false, "error": format!("{e:#}")}),
        };
        let mut bytes = serde_json::to_vec(&value)?;
        bytes.push(b'\n');
        let mut written = 0;
        unsafe {
            WriteFile(
                self.handle.0,
                bytes.as_ptr(),
                bytes.len() as u32,
                &mut written,
                null_mut(),
            );
        }
        // Do not block SCM stop on a client that never reads. The response fits the pipe buffer.
        self.replied = true;
        self.deadline = Instant::now() + Duration::from_secs(2);
        Ok(())
    }
}

fn status(handle: SERVICE_STATUS_HANDLE, state: u32, exit: u32) -> Result<()> {
    let value = SERVICE_STATUS {
        dwServiceType: SERVICE_WIN32_OWN_PROCESS,
        dwCurrentState: state,
        dwControlsAccepted: if state == SERVICE_RUNNING {
            SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN
        } else {
            0
        },
        dwWin32ExitCode: exit,
        dwServiceSpecificExitCode: 0,
        dwCheckPoint: if state == SERVICE_START_PENDING { 1 } else { 0 },
        dwWaitHint: if state == SERVICE_START_PENDING {
            10000
        } else {
            0
        },
    };
    unsafe { check(SetServiceStatus(handle, &value)) }
}
unsafe extern "system" fn control(code: u32, _: u32, _: *mut c_void, _: *mut c_void) -> u32 {
    match code {
        SERVICE_CONTROL_STOP | SERVICE_CONTROL_SHUTDOWN => {
            STOP.store(true, Ordering::SeqCst);
            NO_ERROR
        }
        SERVICE_CONTROL_INTERROGATE => NO_ERROR,
        _ => ERROR_CALL_NOT_IMPLEMENTED,
    }
}
fn serve(path: &Path, service: Option<SERVICE_STATUS_HANDLE>) -> Result<()> {
    let cfg = Config::load(path)?;
    let mut pipe = Pipe::bind(&cfg)?;
    let mut runtime = Runtime::open(cfg)?;
    if let Some(handle) = service {
        status(handle, SERVICE_RUNNING, NO_ERROR)?;
    }
    while !STOP.load(Ordering::SeqCst) {
        pipe.poll(&mut runtime)?;
        runtime.poll()?;
        thread::sleep(Duration::from_millis(25));
    }
    // Preserve desired running state on normal service stop/restart, but terminate all descendants.
    runtime.child.take();
    Ok(())
}
unsafe extern "system" fn service_main(_: u32, _: *mut *mut u16) {
    let handle = RegisterServiceCtrlHandlerExW(wide(SERVICE).as_ptr(), Some(control), null());
    if handle.is_null() {
        return;
    }
    let result = (|| -> Result<()> {
        status(handle, SERVICE_START_PENDING, NO_ERROR)?;
        let path = std::env::args_os()
            .nth(2)
            .context("missing service config path")?;
        serve(Path::new(&path), Some(handle))
    })();
    // Never emit configuration or credentials to service logs.
    let _ = status(
        handle,
        SERVICE_STOPPED,
        if result.is_ok() {
            NO_ERROR
        } else {
            ERROR_SERVICE_SPECIFIC_ERROR
        },
    );
}
pub fn main() -> Result<()> {
    ensure!(cfg!(target_arch = "x86_64"), "Windows x86_64 only");
    let args: Vec<_> = std::env::args_os().collect();
    match args.get(1).and_then(|s| s.to_str()) {
        Some("--service") if args.len() == 3 => {
            let name = wide(SERVICE);
            let table = [SERVICE_TABLE_ENTRYW { lpServiceName: name.as_ptr().cast_mut(), lpServiceProc: Some(service_main) }, SERVICE_TABLE_ENTRYW { lpServiceName: null_mut(), lpServiceProc: None }];
            unsafe { check(StartServiceCtrlDispatcherW(table.as_ptr())) }
        }
        Some("--console") if args.len() == 3 => serve(Path::new(&args[2]), None),
        Some("--request") if args.len() == 4 => {
            let pipe = args[2].to_str().context("pipe name")?;
            ensure!(pipe.starts_with("suifleet-") && pipe.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-'), "invalid pipe name");
            let bytes = fs::read(&args[3])?; ensure!(bytes.len() <= 65535, "request too large");
            let _: Request = serde_json::from_slice(&bytes)?;
            let path = format!(r"\\.\pipe\{pipe}");
            unsafe { check(WaitNamedPipeW(wide(&path).as_ptr(), 5000))?; }
            use std::os::windows::fs::OpenOptionsExt;
            let mut client = fs::OpenOptions::new().read(true).write(true).access_mode(0x12019b).open(path)?;
            client.write_all(&bytes)?; client.write_all(b"\n")?;
            let mut response = String::new();
            use std::io::BufRead;
            std::io::BufReader::new(&mut client).read_line(&mut response)?;
            client.write_all(b"ack\n")?;
            let value: serde_json::Value = serde_json::from_str(&response)?;
            println!("{response}"); ensure!(value["ok"] == true, "request rejected"); Ok(())
        }
        _ => bail!("usage: suifleet --service CONFIG | --console CONFIG | --request PIPE REQUEST_JSON; Linux-only APIs are unsupported"),
    }
}
