//! Content-free diagnostics. Never serialize Display/Debug from the error chain.
#[derive(Debug, Clone, Copy)]
pub enum Stage {
    Recovery,
    Reports,
    Watch,
    Manifest,
    Authority,
    Baseline,
    Discovery,
    Announcements,
    Events,
    Reconcile,
    Journal,
    Deferred,
}
impl Stage {
    pub fn label(self) -> &'static str {
        match self {
            Self::Recovery => "recovery",
            Self::Reports => "reports",
            Self::Watch => "watch",
            Self::Manifest => "manifest",
            Self::Authority => "authority",
            Self::Baseline => "baseline",
            Self::Discovery => "discovery",
            Self::Announcements => "announcements",
            Self::Events => "events",
            Self::Reconcile => "reconcile",
            Self::Journal => "journal",
            Self::Deferred => "deferred",
        }
    }
}
// Keep the original outer Display unchanged for local logs and health state.
// The private text is never used as a public label.
#[derive(Debug)]
struct Context {
    stage: Stage,
    private_message: String,
}
impl std::fmt::Display for Context {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.private_message)
    }
}
impl std::error::Error for Context {}

pub trait PollContext<T> {
    fn poll_stage(self, stage: Stage) -> anyhow::Result<T>;
}
impl<T> PollContext<T> for anyhow::Result<T> {
    fn poll_stage(self, stage: Stage) -> anyhow::Result<T> {
        self.map_err(|error| {
            let private_message = error.to_string();
            error.context(Context {
                stage,
                private_message,
            })
        })
    }
}

// These are hints, not proof of a provider failure or permission to retry writes.
// Match every cause independently: outer object/context messages hide RPC status.
const MARKERS: &[(&str, &[&str])] = &[
    ("timeout", &["timeout", "timed out", "deadline exceeded"]),
    (
        "rate-limited",
        &["429", "resource exhausted", "resourceexhausted"],
    ),
    (
        "unavailable",
        &["unavailable", "connection refused", "connection reset"],
    ),
    (
        "unauthorized",
        &[
            "permission denied",
            "unauthenticated",
            "not owned by device key",
        ],
    ),
    ("not-found", &["not found", "notfound", "deleted or absent"]),
    ("invalid-argument", &["invalid argument", "invalidargument"]),
    ("unimplemented", &["unimplemented"]),
    (
        "internal",
        &["internal error", "internal:", "internal server"],
    ),
    (
        "decode",
        &[
            "undecodable",
            "decoding",
            "deserialize",
            "invalid byte vector",
        ],
    ),
    (
        "missing-data",
        &[
            "has no data",
            "has no contents",
            "has no version",
            "without a",
            "missing discovery",
        ],
    ),
    ("watch-batch", &["watch read failed", "incomplete watch"]),
    ("chain-mismatch", &["chain identifier mismatch"]),
    (
        "authority",
        &["authority", "capability", "unauthorized target"],
    ),
    (
        "schema",
        &[
            "schema version",
            "type origin",
            "unexpected",
            "not a move object",
        ],
    ),
    ("history-pruned", &["pruned", "lowest available checkpoint"]),
    (
        "event-stream",
        &[
            "event stream",
            "event cursor",
            "resume cursor",
            "discovery fence",
        ],
    ),
    ("integrity", &["sha-256 mismatch", "digest mismatch"]),
    ("tls", &["certificate", "tls", "handshake"]),
    (
        "storage",
        &["no space", "read-only file system", "os error"],
    ),
];

pub fn classify(error: &anyhow::Error) -> (&'static str, Vec<&'static str>) {
    let stage = error
        .downcast_ref::<Context>()
        .map_or("poll", |s| s.stage.label());
    let causes: Vec<_> = error
        .chain()
        .map(|cause| cause.to_string().to_ascii_lowercase())
        .collect();
    let mut categories: Vec<_> = MARKERS
        .iter()
        .filter_map(|(label, markers)| {
            causes
                .iter()
                .any(|cause| markers.iter().any(|m| cause.contains(m)))
                .then_some(*label)
        })
        .collect();
    if categories.is_empty() {
        categories.push("other");
    }
    (stage, categories)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn wrapped_causes_are_classified_without_exporting_content() {
        let error = anyhow::anyhow!(
            "Unavailable: PRIVATE-KEY token=https://secret.invalid/0xdead cursor=private timeout"
        )
        .context("object 0xdead private configuration");
        let error = Err::<(), _>(error)
            .poll_stage(Stage::Announcements)
            .unwrap_err();
        let (stage, categories) = classify(&error);
        assert_eq!(stage, "announcements");
        assert_eq!(categories, ["timeout", "unavailable"]);
        assert!(!format!("{stage} {categories:?}").contains("private"));
        assert_eq!(error.chain().count(), 3);
        assert_eq!(error.to_string(), "object 0xdead private configuration");
    }
    #[test]
    fn unknown_and_duplicate_causes_stay_bounded() {
        assert_eq!(
            classify(&anyhow::anyhow!("secret 0xdead")),
            ("poll", vec!["other"])
        );
        let error =
            Err::<(), _>(anyhow::anyhow!("RPC request timeout secret").context("timeout secret"))
                .poll_stage(Stage::Watch)
                .unwrap_err();
        assert_eq!(classify(&error), ("watch", vec!["timeout"]));
    }
    #[test]
    fn context_preserves_typed_control_errors() {
        #[derive(Debug)]
        struct Control;
        impl std::fmt::Display for Control {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                write!(f, "control")
            }
        }
        impl std::error::Error for Control {}
        let error = Err::<(), _>(anyhow::Error::new(Control))
            .poll_stage(Stage::Reconcile)
            .unwrap_err();
        assert!(error.downcast_ref::<Control>().is_some());
    }
}
