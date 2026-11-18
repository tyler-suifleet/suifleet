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
// Stage metadata stays beside the error so Display, Debug, chains and typed
// control errors retain their original behavior, including bounded health text.
pub type PollStage = std::cell::Cell<Option<Stage>>;

pub trait PollContext<T> {
    fn poll_stage(self, context: &PollStage, stage: Stage) -> anyhow::Result<T>;
}
impl<T> PollContext<T> for anyhow::Result<T> {
    fn poll_stage(self, context: &PollStage, stage: Stage) -> anyhow::Result<T> {
        if self.is_err() {
            context.set(Some(stage));
        }
        self
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

pub fn classify(error: &anyhow::Error, stage: Option<Stage>) -> (&'static str, Vec<&'static str>) {
    let stage = stage.map_or("poll", Stage::label);
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
        let display = error.to_string();
        let alternate = format!("{error:#}");
        let debug = format!("{error:?}");
        let mut expected_health = crate::telemetry::SyncHealth::default();
        let original = Err::<(), _>(error);
        expected_health.record(&original);
        let context = PollStage::default();
        let error = original
            .poll_stage(&context, Stage::Announcements)
            .unwrap_err();
        let (stage, categories) = classify(&error, context.get());
        assert_eq!(stage, "announcements");
        assert_eq!(categories, ["timeout", "unavailable"]);
        assert!(!format!("{stage} {categories:?}").contains("private"));
        assert_eq!(error.chain().count(), 2);
        assert_eq!(error.to_string(), display);
        assert_eq!(format!("{error:#}"), alternate);
        assert_eq!(format!("{error:?}"), debug);
        let mut health = crate::telemetry::SyncHealth::default();
        health.record(&Err(error));
        assert_eq!(health, expected_health);
        assert!(health.last_error.unwrap().ends_with("timeout"));
    }
    #[test]
    fn unknown_and_duplicate_causes_stay_bounded() {
        assert_eq!(
            classify(&anyhow::anyhow!("secret 0xdead"), None),
            ("poll", vec!["other"])
        );
        let context = PollStage::default();
        let error =
            Err::<(), _>(anyhow::anyhow!("RPC request timeout secret").context("timeout secret"))
                .poll_stage(&context, Stage::Watch)
                .unwrap_err();
        assert_eq!(classify(&error, context.get()), ("watch", vec!["timeout"]));
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
        let context = PollStage::default();
        let error = Err::<(), _>(anyhow::Error::new(Control))
            .poll_stage(&context, Stage::Reconcile)
            .unwrap_err();
        assert!(error.downcast_ref::<Control>().is_some());
    }
}
