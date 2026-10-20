//! Harmless executable used by the real Windows service acceptance suite.
#[cfg(windows)]
fn main() -> Result<(), Box<dyn std::error::Error>> {
    use std::{
        fs,
        io::{BufRead, Write},
        os::windows::{fs::OpenOptionsExt, process::CommandExt},
        process::Command,
        thread,
        time::Duration,
    };
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("probe") => {
            let pipe_denied = fs::OpenOptions::new()
                .read(true)
                .write(true)
                .access_mode(0x12019b)
                .security_qos_flags(0x10000)
                .open(format!(r"\\.\pipe\{}", args[2]))
                .err()
                .is_some_and(|e| e.raw_os_error() == Some(5));
            let file_denied = fs::read(&args[3])
                .err()
                .is_some_and(|e| e.kind() == std::io::ErrorKind::PermissionDenied);
            fs::write(
                &args[4],
                if pipe_denied && file_denied {
                    "denied"
                } else {
                    "FAILED"
                },
            )?;
        }
        Some("install") => {
            fs::write("installed", b"installed")?;
        }
        Some("fail") => std::process::exit(17),
        Some("hang") => {
            fs::write("install-started", b"started")?;
            loop {
                thread::sleep(Duration::from_secs(1));
            }
        }
        Some("shutdown") => {
            fs::write("shutdown", b"shutdown")?;
        }
        Some("child") => {
            fs::write("descendant.pid", std::process::id().to_string())?;
            loop {
                thread::sleep(Duration::from_secs(1));
            }
        }
        Some("run") => {
            let root = std::path::Path::new(&args[2]);
            let pipe = &args[3];
            let secret_denied = fs::read(root.join("config.json"))
                .err()
                .is_some_and(|e| e.kind() == std::io::ErrorKind::PermissionDenied);
            let write_denied = fs::write(root.join("state.json"), b"bad")
                .err()
                .is_some_and(|e| e.kind() == std::io::ErrorKind::PermissionDenied);
            let breakaway_denied = Command::new(std::env::current_exe()?)
                .arg("child")
                .creation_flags(0x01000000)
                .spawn()
                .is_err();
            let deadline = std::time::Instant::now() + Duration::from_secs(8);
            let mut client = loop {
                match fs::OpenOptions::new()
                    .read(true)
                    .write(true)
                    .access_mode(0x12019b)
                    .security_qos_flags(0x10000)
                    .open(format!(r"\\.\pipe\{pipe}"))
                {
                    Ok(client) => break client,
                    Err(e) if std::time::Instant::now() < deadline => {
                        let _ = e;
                        thread::sleep(Duration::from_millis(50));
                    }
                    Err(e) => return Err(e.into()),
                }
            };
            client.write_all(b"{\"operation\":\"stop\"}\n")?;
            let mut response = String::new();
            std::io::BufReader::new(&mut client).read_line(&mut response)?;
            client.write_all(b"ack\n")?;
            let operator_denied = response.contains("operator permission required");
            fs::write("denials.json", format!("{{\"secret\":{secret_denied},\"state_write\":{write_denied},\"breakaway\":{breakaway_denied},\"operator\":{operator_denied}}}"))?;
            let mut child = Command::new(std::env::current_exe()?)
                .arg("child")
                .spawn()?;
            fs::write("parent.pid", std::process::id().to_string())?;
            if args.get(4).map(String::as_str) == Some("crash") {
                thread::sleep(Duration::from_millis(500));
                std::process::exit(19);
            }
            child.wait()?;
        }
        _ => return Err("unknown fixture operation".into()),
    }
    Ok(())
}
#[cfg(not(windows))]
fn main() {
    panic!("Windows acceptance fixture only");
}
