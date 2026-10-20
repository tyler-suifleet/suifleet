//! Native Windows software runtime. No AWS implementation code is embedded.
use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};

#[cfg(windows)]
mod platform;
#[cfg(windows)]
pub use platform::main;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub schema_version: u32,
    pub name: String,
    pub version: String,
    pub platform: String,
    pub sha256: String,
    pub run: Vec<String>,
    #[serde(default)]
    pub install: Option<Vec<String>>,
    #[serde(default)]
    pub shutdown: Option<Vec<String>>,
    pub timeout_seconds: u32,
    pub max_restarts: u32,
}
impl Manifest {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            self.schema_version == 1,
            "unsupported Windows manifest schema"
        );
        ensure!(
            self.platform == "windows-x86_64",
            "unsupported component platform"
        );
        for value in [&self.name, &self.version] {
            ensure!(
                !value.is_empty()
                    && value.len() <= 100
                    && value
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"-_.".contains(&c))
                    && value != "."
                    && value != "..",
                "invalid component identity"
            );
        }
        ensure!(
            self.sha256.len() == 64 && hex::decode(&self.sha256)?.len() == 32,
            "invalid digest"
        );
        ensure!(
            (1..=300).contains(&self.timeout_seconds) && self.max_restarts <= 10,
            "invalid lifecycle limits"
        );
        for args in std::iter::once(&self.run)
            .chain(self.install.iter())
            .chain(self.shutdown.iter())
        {
            ensure!(
                args.len() <= 64 && args.iter().all(|s| !s.contains('\0') && s.len() <= 4096),
                "invalid arguments"
            );
        }
        Ok(())
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, tag = "operation", rename_all = "snake_case")]
pub enum Request {
    Status {},
    Install {
        manifest: Manifest,
        artifact: String,
    },
    Stop {},
    Start {},
}

/// Win32 CRT argument quoting, with an explicit application path (no shell).
pub fn quote(arg: &str) -> String {
    let mut out = String::from("\"");
    let mut slashes = 0;
    for c in arg.chars() {
        if c == '\\' {
            slashes += 1;
            continue;
        }
        if c == '"' {
            out.extend(std::iter::repeat_n('\\', slashes * 2 + 1));
        } else {
            out.extend(std::iter::repeat_n('\\', slashes));
        }
        slashes = 0;
        out.push(c);
    }
    out.extend(std::iter::repeat_n('\\', slashes * 2));
    out.push('"');
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    fn valid() -> Manifest {
        Manifest {
            schema_version: 1,
            name: "app".into(),
            version: "1".into(),
            platform: "windows-x86_64".into(),
            sha256: "ab".repeat(32),
            run: vec![],
            install: None,
            shutdown: None,
            timeout_seconds: 5,
            max_restarts: 2,
        }
    }
    #[test]
    fn rejects_unsupported_and_unsafe_manifests() {
        let mut m = valid();
        m.validate().unwrap();
        m.platform = "linux-x86_64".into();
        assert!(m.validate().is_err());
        m = valid();
        m.name = "..".into();
        assert!(m.validate().is_err());
        m = valid();
        m.max_restarts = 11;
        assert!(m.validate().is_err());
        m = valid();
        m.run = vec!["bad\0arg".into()];
        assert!(m.validate().is_err());
        m = valid();
        m.schema_version = 2;
        assert!(m.validate().is_err());
        m = valid();
        m.sha256 = "x".repeat(64);
        assert!(m.validate().is_err());
        assert!(serde_json::from_str::<Request>(r#"{"operation":"reboot"}"#).is_err());
        assert!(
            serde_json::from_str::<Request>(r#"{"operation":"status","sid":"spoof"}"#).is_err()
        );
    }
    #[test]
    fn quotes_windows_arguments() {
        assert_eq!(quote("a b"), "\"a b\"");
        assert_eq!(quote("a\"b"), "\"a\\\"b\"");
        assert_eq!(quote("a\\"), "\"a\\\\\"");
    }
}
