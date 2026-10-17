use super::*;
use serde_json::{json, Value};
use std::io::BufRead;

fn fresh(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("suifleet-cwlogs-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn append(path: &Path, text: &str) {
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::OpenOptions::new().create(true).append(true).open(path).unwrap().write_all(text.as_bytes()).unwrap();
}

fn settings(sources: &[(&str, ComponentType, Level)]) -> Settings {
    Settings {
        region: "us-east-1".into(),
        thing: "thing".into(),
        interval_seconds: 300.0,
        spool_limit: DEFAULT_SPOOL_LIMIT,
        redact: Vec::new(),
        sources: sources.iter().map(|(n, t, l)| (n.to_string(), Source { component_type: *t, minimum: *l })).collect(),
    }
}

fn messages(exporter: &Exporter) -> Vec<(String, i64, String)> {
    exporter.spool.events.values().map(|e| (e.source.clone(), e.ms, e.message.clone())).collect()
}

#[test]
fn dates_and_leading_times() {
    assert_eq!(day(0), "1970/01/01");
    assert_eq!(day(1_759_967_999_000), "2025/10/08");
    assert_eq!(day(1_759_968_000_000), "2025/10/09");
    assert_eq!(day((TIME_LIMIT_SECONDS - 1) * 1000), "9999/12/31");
    for days in [-1_000_000i64, -1, 0, 1, 59, 60, 10_957, 2_932_896] {
        let (y, m, d) = civil(days);
        assert_eq!(days_from_civil(y, m, d), days);
    }
    assert_eq!(amz_date(1_440_938_160_000), "20150830T123600Z");
    assert_eq!(leading_time("2025-10-09T00:00:01.5Z rest"), Some((1_759_968_001_500, " rest")));
    assert_eq!(leading_time("2025-10-09T00:00:01.123456Z  INFO x"), Some((1_759_968_001_123, "  INFO x")));
    assert_eq!(leading_time("2025-10-09 00:00:01+00:00 x"), Some((1_759_968_001_000, " x")));
    assert_eq!(leading_time("2025-10-09T00:00:01+02:00 x"), None, "only UTC is read");
    assert_eq!(leading_time("2025-13-09T00:00:01Z"), None);
    assert_eq!(leading_time("not a time at all, really"), None);
    assert_eq!(classify("2025-10-09T00:00:01Z  WARN suifleet: disk", 7), (Some(Level::Warn), 1_759_968_001_000));
    assert_eq!(classify("2025-10-09T00:00:01.000Z [ERROR] (main) boom", 7).0, Some(Level::Error));
    assert_eq!(classify("DEBUG: detail", 7), (Some(Level::Debug), 7));
    assert_eq!(classify("WARNING low", 7).0, Some(Level::Warn));
    assert_eq!(classify("TRACE noise", 7).0, None);
    assert_eq!(classify("plain output", 7), (Some(Level::Info), 7));
    assert_eq!(classify("", 7), (Some(Level::Info), 7));
}

#[test]
fn split_cuts_only_at_utf8_boundaries() {
    let limit = MAX_EVENT_BYTES - EVENT_OVERHEAD;
    assert_eq!(split(&"x".repeat(limit)).len(), 1);
    assert_eq!(split(&"x".repeat(limit + 1)).len(), 2);
    let euro = "\u{20ac}".repeat(87_373);
    let parts = split(&euro);
    assert_eq!(parts.len(), 2);
    assert_eq!(parts.concat(), euro);
    assert!(parts.iter().all(|p| p.len() <= limit && p.len() % 3 == 0));
    let mixed = format!("{}\u{1f600}{}", "a".repeat(limit - 2), "b".repeat(10));
    let parts = split(&mixed);
    assert_eq!(parts[0].len(), limit - 2, "the four-byte character moves whole to the next event");
    assert_eq!(parts.concat(), mixed);
    assert_eq!(split(&"x".repeat(1_048_576)).len(), 5);
}

#[test]
fn hmac_and_sigv4_match_published_vectors() {
    // RFC 4231, test case 2.
    assert_eq!(hex::encode(hmac(b"Jefe", b"what do ya want for nothing?")), "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843");
    // RFC 4231, test case 6 (a key longer than the block).
    assert_eq!(
        hex::encode(hmac(&[0xaa; 131], b"Test Using Larger Than Block-Size Key - Hash Key First")),
        "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54"
    );
    // AWS Signature Version 4 documentation example (IAM ListUsers).
    let secret = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY";
    assert_eq!(hex::encode(signing_key(secret, "20150830", "us-east-1", "iam")), "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9");
    let canonical = "GET\n/\nAction=ListUsers&Version=2010-05-08\ncontent-type:application/x-www-form-urlencoded; charset=utf-8\nhost:iam.amazonaws.com\nx-amz-date:20150830T123600Z\n\ncontent-type;host;x-amz-date\ne3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
    assert_eq!(sha256_hex(canonical.as_bytes()), "f536975d06c0309214f805bb90ccff089219ecd68b2577efef23edd43b7e1a59");
    let to_sign = string_to_sign("20150830T123600Z", "20150830/us-east-1/iam/aws4_request", canonical);
    let key = signing_key(secret, "20150830", "us-east-1", "iam");
    assert_eq!(hex::encode(hmac(&key, to_sign.as_bytes())), "5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7");
}

#[test]
fn signed_headers_never_print_the_secret() {
    let credentials = Credentials { access_key_id: "AKID".into(), secret_access_key: "very-secret".into(), token: "tok".into(), expires_ms: 0 };
    let headers = sign(&credentials, "eu-west-1", "logs.eu-west-1.amazonaws.com", "Logs_20140328.PutLogEvents", b"{}", 1_440_938_160_000);
    let authorization = &headers.iter().find(|(n, _)| *n == "authorization").unwrap().1;
    assert!(authorization.starts_with("AWS4-HMAC-SHA256 Credential=AKID/20150830/eu-west-1/logs/aws4_request, SignedHeaders=content-type;host;x-amz-date;x-amz-security-token;x-amz-target, Signature="));
    assert!(!format!("{headers:?}{credentials:?}").contains("very-secret"));
    assert_eq!(format!("{credentials:?}"), "Credentials([REDACTED])");
    let source = CredentialSource::new("http://127.0.0.1:9/2016-11-01/credentialprovider/", "token-value".into()).unwrap();
    assert!(!format!("{source:?}").contains("token-value"));
    assert!(CredentialSource::new("http://example.com/creds", "t".into()).is_err(), "plain HTTP only to loopback");
    assert!(CredentialSource::new("https://example.com/creds", "t".into()).is_ok());
    assert!(CredentialSource::new("http://[::1]:8/creds", "t".into()).is_ok());
    assert!(CredentialSource::new("http://127.0.0.1:8/creds", "a\nb".into()).is_err());
}

#[test]
fn batches_respect_count_bytes_and_day() {
    let event = |seq: u64, ms: i64, len: usize| Event { seq, source: "a".into(), ms, message: "x".repeat(len) };
    let many: Vec<Event> = (0..10_001).map(|i| event(i, 1000, 1)).collect();
    let refs: Vec<&Event> = many.iter().collect();
    let out = batches(&refs);
    assert_eq!(out.iter().map(|(_, b)| b.len()).collect::<Vec<_>>(), [10_000, 1]);
    // Four events of 262,144 bytes fill 1,048,576 exactly; a fifth starts a new batch.
    let big: Vec<Event> = (0..5).map(|i| event(i, 1000, MAX_EVENT_BYTES - EVENT_OVERHEAD)).collect();
    let refs: Vec<&Event> = big.iter().collect();
    assert_eq!(batches(&refs).iter().map(|(_, b)| b.len()).collect::<Vec<_>>(), [4, 1]);
    let days = [event(0, DAY_MS - 1, 1), event(1, DAY_MS, 1), event(2, DAY_MS + 1, 1)];
    let refs: Vec<&Event> = days.iter().collect();
    let out = batches(&refs);
    assert_eq!(out.iter().map(|(d, b)| (d.as_str(), b.len())).collect::<Vec<_>>(), [("1970/01/01", 1), ("1970/01/02", 2)]);
}

#[test]
fn partially_rejected_events_are_counted() {
    assert_eq!(rejected_events(br#"{"nextSequenceToken":"1"}"#, 5), 0);
    assert_eq!(rejected_events(br#"{"rejectedLogEventsInfo":{"tooOldLogEventEndIndex":1}}"#, 5), 2);
    assert_eq!(rejected_events(br#"{"rejectedLogEventsInfo":{"tooNewLogEventStartIndex":3}}"#, 5), 2);
    assert_eq!(rejected_events(br#"{"rejectedLogEventsInfo":{"expiredLogEventEndIndex":0,"tooOldLogEventEndIndex":2,"tooNewLogEventStartIndex":4}}"#, 5), 4);
    assert_eq!(rejected_events(br#"{"rejectedLogEventsInfo":{"tooOldLogEventEndIndex":9}}"#, 5), 5);
    assert_eq!(rejected_events(b"not json", 5), 0);
}

#[test]
fn configuration_uses_logmanager_names_and_rejects_the_rest() {
    let d = fresh("config");
    let write = |value: Value| {
        let path = d.join("config.json");
        std::fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
        FileConfig::load(&path)
    };
    let base = json!({
        "region": "eu-west-1", "thingName": "fleet:pi-1", "stateDir": "/var/lib/suifleet", "spoolDir": "/var/lib/suifleet-cloudwatch",
        "logsUploaderConfiguration": {
            "systemLogsConfiguration": {"uploadToCloudWatch": true, "minimumLogLevel": "WARN"},
            "componentLogsConfigurationMap": {"com.example.App": {}, "sensor": {"minimumLogLevel": "DEBUG"}}
        }
    });
    let config = write(base.clone()).unwrap();
    let s = config.settings().unwrap();
    assert_eq!((s.interval_seconds, s.spool_limit), (300.0, DEFAULT_SPOOL_LIMIT));
    assert_eq!(s.sources["System"], Source { component_type: ComponentType::GreengrassSystemComponent, minimum: Level::Warn });
    assert_eq!(s.sources["com.example.App"], Source { component_type: ComponentType::UserComponent, minimum: Level::Info });
    assert_eq!(s.sources["sensor"].minimum, Level::Debug);
    assert_eq!(s.group("System"), "/aws/greengrass/GreengrassSystemComponent/eu-west-1/System");
    assert_eq!(s.stream("2025/10/09"), "/2025/10/09/thing/fleet+pi-1");
    assert_eq!(config.endpoint().unwrap().as_str(), "https://logs.eu-west-1.amazonaws.com/");
    let mut off = base.clone();
    off["logsUploaderConfiguration"]["systemLogsConfiguration"]["uploadToCloudWatch"] = json!(false);
    assert!(!write(off).unwrap().settings().unwrap().sources.contains_key("System"));
    // Unsupported LogManager options and unknown keys fail rather than being ignored.
    for (pointer, value) in [
        ("/logsUploaderConfiguration/componentLogsConfigurationMap/sensor/logFileRegex", json!("x.log")),
        ("/logsUploaderConfiguration/componentLogsConfigurationMap/sensor/deleteLogFileAfterCloudUpload", json!(true)),
        ("/logsUploaderConfiguration/systemLogsConfiguration/diskSpaceLimit", json!(1)),
        ("/logsUploaderConfiguration/componentLogsConfigurationMap/sensor/minimumLogLevel", json!("TRACE")),
        ("/logsUploaderConfiguration/componentLogsConfigurationMap/System", json!({})),
        ("/logsUploaderConfiguration/componentLogsConfigurationMap/..", json!({})),
        ("/endpoint", json!("http://logs.example.com/")),
        ("/endpoint", json!("https://logs.example.com/prefix")),
        ("/periodicUploadIntervalSec", json!(0)),
        ("/spoolLimitBytes", json!(0)),
        ("/readIntervalSec", json!(0)),
        ("/redact", json!([""])),
        ("/spoolDir", json!("/var/lib/suifleet/spool")),
        ("/spoolDir", json!("relative")),
        ("/thingName", json!("bad*thing")),
        ("/region", json!("EU")),
        ("/credentials", json!({})),
    ] {
        let mut changed = base.clone();
        let (parent, key) = pointer.rsplit_once('/').unwrap();
        let target = if parent.is_empty() { &mut changed } else { changed.pointer_mut(parent).unwrap() };
        target.as_object_mut().unwrap().insert(key.to_owned(), value);
        assert!(write(changed).is_err(), "{pointer} accepted");
    }
    let mut local = base.clone();
    local["endpoint"] = json!("http://127.0.0.1:4566/");
    assert!(write(local).is_ok(), "plain HTTP to a loopback emulator is allowed");
}

#[test]
fn spool_survives_restart_and_discards_only_a_torn_tail() {
    let d = fresh("spool");
    let s = settings(&[("app", ComponentType::UserComponent, Level::Info)]);
    let mut exporter = Exporter::open(&d, s.clone()).unwrap();
    for (i, text) in ["one", "two", "three"].iter().enumerate() {
        assert_eq!(exporter.log("app", "INFO", text, i as f64).unwrap(), LogStatus::Spooled { events: 1 });
    }
    drop(exporter);
    let journal = d.join(JOURNAL);
    assert_eq!(std::fs::metadata(&journal).unwrap().permissions().mode() & 0o777, 0o600);
    assert_eq!(std::fs::metadata(&d).unwrap().permissions().mode() & 0o777, 0o700);
    // A crash in the middle of a record: that record alone is lost.
    append(&journal, r#"{"pass":{"events":[{"seq":4,"source":"TORN","ms":4,"mess"#);
    let exporter = Exporter::open(&d, s.clone()).unwrap();
    assert_eq!(messages(&exporter).iter().map(|m| m.2.as_str()).collect::<Vec<_>>(), ["one", "two", "three"]);
    drop(exporter);
    assert!(!std::fs::read_to_string(&journal).unwrap().contains("TORN"), "the torn record was truncated");
    // Anything else unreadable fails closed.
    let text = std::fs::read_to_string(&journal).unwrap();
    std::fs::write(&journal, format!("garbage\n{text}")).unwrap();
    assert!(Exporter::open(&d, s.clone()).is_err());
    std::fs::write(&journal, text).unwrap();
    // Events of a source no longer configured are dropped and counted.
    let other = settings(&[("b", ComponentType::UserComponent, Level::Info)]);
    let exporter = Exporter::open(&d, other.clone()).unwrap();
    assert_eq!((exporter.spool.len(), exporter.spool.dropped), (0, 3));
    drop(exporter);
    assert_eq!(Exporter::open(&d, other).unwrap().spool.dropped, 3);
}

#[test]
fn spool_compacts_without_losing_state() {
    let d = fresh("compact");
    let mut s = settings(&[("app", ComponentType::UserComponent, Level::Info)]);
    s.spool_limit = 200_000;
    let mut exporter = Exporter::open(&d, s.clone()).unwrap();
    let line = "y".repeat(10_000);
    for i in 0..400 {
        exporter.log("app", "INFO", &line, i as f64).unwrap();
    }
    let journal = std::fs::metadata(d.join(JOURNAL)).unwrap().len();
    assert!(journal < 3 * 1024 * 1024, "the journal was compacted ({journal} bytes)");
    let (spooled, dropped, bytes) = (exporter.spool.len(), exporter.spool.dropped, exporter.spool.bytes());
    assert!(bytes <= 200_000 && dropped == 400 - spooled as u64, "{spooled} {dropped} {bytes}");
    drop(exporter);
    let reopened = Exporter::open(&d, s).unwrap();
    assert_eq!((reopened.spool.len(), reopened.spool.dropped, reopened.spool.bytes()), (spooled, dropped, bytes));
    assert_eq!(reopened.spool.events.keys().next(), Some(&(400 - spooled as u64 + 1)), "the oldest were dropped");
}

#[test]
fn write_failures_leave_the_spool_unchanged() {
    if unsafe { libc::geteuid() } == 0 {
        return; // root ignores the permission this test relies on
    }
    let d = fresh("readonly");
    let s = settings(&[("app", ComponentType::UserComponent, Level::Info)]);
    let mut exporter = Exporter::open(&d, s.clone()).unwrap();
    exporter.log("app", "INFO", "kept", 1.0).unwrap();
    // A journal that can no longer be written: the line is refused, not half-kept.
    exporter.spool.file = std::fs::File::open(d.join(JOURNAL)).unwrap();
    assert!(exporter.log("app", "INFO", "refused", 2.0).is_err());
    assert_eq!(messages(&exporter).len(), 1);
    drop(exporter);
    assert_eq!(messages(&Exporter::open(&d, s).unwrap()).len(), 1);
}

// --- Collection from the daemon's files ------------------------------------------

fn retained(state: &Path, values: &[&str]) {
    std::fs::create_dir_all(state.join("logs")).unwrap();
    let values: serde_json::Map<String, Value> = values.iter().map(|v| (v.to_string(), json!({}))).collect();
    std::fs::write(state.join("logs/confidential.json"), serde_json::to_vec(&json!({"values": values})).unwrap()).unwrap();
}

fn files(state: &Path) -> SourceFiles {
    SourceFiles { state_dir: state.to_owned(), confidential: true, start: BTreeMap::new() }
}

#[test]
fn collection_redacts_classifies_and_reads_each_line_once() {
    let root = fresh("collect");
    let state = root.join("state");
    retained(&state, &["Ov3rlay-PIN"]);
    let s = settings(&[
        ("System", ComponentType::GreengrassSystemComponent, Level::Info),
        ("svc", ComponentType::UserComponent, Level::Info),
    ]);
    let mut exporter = Exporter::open(&root.join("spool"), s.clone()).unwrap();
    let svc = state.join("supervised/svc/component.log");
    append(&svc, "connecting with Ov3rlay-PIN\nDEBUG hidden\napi_key=k3y-abcdef0123\n-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n-----END PRIVATE KEY-----\n\nplain\npartial");
    append(&state.join("logs/daemon.log"), "2025-10-09T00:00:01.250Z  WARN suifleet: disk low\n2025-10-09T00:00:02Z TRACE noise\n");
    // A sibling that is not configured is never read.
    append(&state.join("supervised/other/component.log"), "sibling secret line\n");
    assert_eq!(exporter.collect(&files(&state), 5_000).unwrap(), 7);
    let got = messages(&exporter);
    assert_eq!(got[0], ("System".into(), 1_759_968_001_250, "2025-10-09T00:00:01.250Z  WARN suifleet: disk low".into()));
    let svc_lines: Vec<&str> = got.iter().filter(|m| m.0 == "svc").map(|m| m.2.as_str()).collect();
    assert_eq!(svc_lines[0], "connecting with [REDACTED]");
    assert_eq!(svc_lines[1], "api_key=[REDACTED]");
    assert!(svc_lines[2..5].iter().all(|l| l.starts_with("[REDACTED]")), "{svc_lines:?}");
    assert_eq!(svc_lines[5], "plain");
    assert!(got.iter().filter(|m| m.0 == "svc").all(|m| m.1 == 5_000), "lines without a time take the read time");
    let spool = std::fs::read_to_string(root.join("spool").join(JOURNAL)).unwrap();
    // The failure message names only which value leaked, never the value.
    for (index, secret) in ["k3y-abcdef0123", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7", "sibling"].iter().enumerate() {
        assert!(!spool.contains(secret), "test value {index} reached the spool");
    }
    assert!(spool.contains("Ov3rlay-PIN"), "confidential values are remembered in the (0600) spool");
    // Nothing new: nothing spooled, the journal does not grow. Then a rotation.
    let before = std::fs::metadata(root.join("spool").join(JOURNAL)).unwrap().len();
    assert_eq!(exporter.collect(&files(&state), 6_000).unwrap(), 0);
    assert_eq!(std::fs::metadata(root.join("spool").join(JOURNAL)).unwrap().len(), before);
    append(&svc, " line\nlast before rotation\n");
    std::fs::rename(&svc, state.join("supervised/svc/component.log.1")).unwrap();
    append(&svc, "after rotation\n");
    // The confidential value left the daemon's set; the adapter still redacts it.
    retained(&state, &[]);
    append(&svc, "late Ov3rlay-PIN\n");
    assert_eq!(exporter.collect(&files(&state), 7_000).unwrap(), 4);
    drop(exporter);
    let exporter = Exporter::open(&root.join("spool"), s).unwrap();
    let svc_lines: Vec<String> = messages(&exporter).into_iter().filter(|m| m.0 == "svc").map(|m| m.2).collect();
    assert_eq!(svc_lines[6..], ["partial line", "last before rotation", "after rotation", "late [REDACTED]"]);
}

#[test]
fn collection_fails_closed_without_confidential_values() {
    let root = fresh("closed");
    let state = root.join("state");
    let s = settings(&[("svc", ComponentType::UserComponent, Level::Info)]);
    let mut exporter = Exporter::open(&root.join("spool"), s).unwrap();
    append(&state.join("supervised/svc/component.log"), "held line\n");
    assert!(exporter.collect(&files(&state), 1).is_err(), "no recorded values: nothing may be read");
    std::fs::create_dir_all(state.join("logs")).unwrap();
    std::fs::write(state.join("logs/confidential.json"), "{\"values\": {\"trunc").unwrap();
    assert!(exporter.collect(&files(&state), 1).is_err(), "a truncated set fails closed");
    assert!(exporter.spool.is_empty() && exporter.spool.cursors.is_empty(), "no line spooled, no cursor moved");
    retained(&state, &[]);
    assert_eq!(exporter.collect(&files(&state), 1).unwrap(), 1);
    let without = SourceFiles { confidential: false, ..files(&root.join("none")) };
    assert!(without.confidential_values().unwrap().is_empty());
}

#[test]
fn collection_refuses_links_out_of_a_component_log() {
    let root = fresh("links");
    let state = root.join("state");
    retained(&state, &[]);
    let s = settings(&[("a", ComponentType::UserComponent, Level::Info), ("b", ComponentType::UserComponent, Level::Info)]);
    let mut exporter = Exporter::open(&root.join("spool"), s).unwrap();
    let sibling = state.join("supervised/sibling/component.log");
    append(&sibling, "sibling private line\n");
    append(&root.join("device.key"), "07070707\n");
    // a: its log is a symbolic link to a sibling's log.
    std::fs::create_dir_all(state.join("supervised/a")).unwrap();
    std::os::unix::fs::symlink(&sibling, state.join("supervised/a/component.log")).unwrap();
    // b: its log is a hard link to another file.
    std::fs::create_dir_all(state.join("supervised/b")).unwrap();
    std::fs::hard_link(root.join("device.key"), state.join("supervised/b/component.log")).unwrap();
    assert_eq!(exporter.collect(&files(&state), 1).unwrap(), 0);
    // A symbolic link higher up (the component's directory) is refused too.
    std::fs::remove_file(state.join("supervised/b/component.log")).unwrap();
    std::fs::remove_dir(state.join("supervised/b")).unwrap();
    std::os::unix::fs::symlink(state.join("supervised/sibling"), state.join("supervised/b")).unwrap();
    // And a rotated `.1` link.
    std::fs::remove_file(state.join("supervised/a/component.log")).unwrap();
    append(&state.join("supervised/a/component.log"), "own line\n");
    assert_eq!(exporter.collect(&files(&state), 1).unwrap(), 1);
    std::fs::rename(state.join("supervised/a/component.log"), root.join("moved")).unwrap();
    std::os::unix::fs::symlink(&sibling, state.join("supervised/a/component.log.1")).unwrap();
    append(&state.join("supervised/a/component.log"), "next own line\n");
    assert_eq!(exporter.collect(&files(&state), 1).unwrap(), 2, "the next line and a lost-rotation marker");
    let got: Vec<String> = messages(&exporter).into_iter().map(|m| m.2).collect();
    assert!(got.iter().all(|m| !m.contains("sibling") && !m.contains("0707")), "{got:?}");
    assert!(got.iter().any(|m| m.contains("rotated away")));
}

#[test]
fn start_cursors_skip_lines_from_before_values_were_recorded() {
    let root = fresh("start");
    let state = root.join("state");
    retained(&state, &[]);
    let log = state.join("supervised/svc/component.log");
    append(&log, "old line from an older daemon\n");
    let (_, end, _) = logs::tail(&log, Cursor::default(), 10).unwrap();
    append(&log, "new line\n");
    let s = settings(&[("svc", ComponentType::UserComponent, Level::Info)]);
    let mut exporter = Exporter::open(&root.join("spool"), s).unwrap();
    let files = SourceFiles { start: [("component:svc".to_owned(), end)].into(), ..files(&state) };
    exporter.collect(&files, 1).unwrap();
    assert_eq!(messages(&exporter).into_iter().map(|m| m.2).collect::<Vec<_>>(), ["new line"]);
}

// --- Declared fixtures against the adapter and the request-checking emulator --------

/// The emulator process (`scripts/cloudwatch_logs_emulator.py --serve`).
struct Emulator {
    child: std::process::Child,
    base: String,
    credentials_uri: String,
    token: String,
    http: reqwest::Client,
}

impl Emulator {
    fn start() -> Emulator {
        let script = Path::new(env!("CARGO_MANIFEST_DIR")).join("../scripts/cloudwatch_logs_emulator.py");
        // Registered, so the supervisor tests' orphan sweep leaves it alone.
        let mut child = crate::supervisor::spawn_std_tracked(
            std::process::Command::new("python3").arg(script).arg("--serve").stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::piped()),
        )
        .expect("python3 runs the CloudWatch Logs emulator");
        let mut line = String::new();
        std::io::BufReader::new(child.stdout.take().unwrap()).read_line(&mut line).unwrap();
        let info: Value = serde_json::from_str(&line).expect("the emulator announces its port");
        Emulator {
            child,
            base: format!("http://127.0.0.1:{}/", info["port"]),
            credentials_uri: info["credentials_uri"].as_str().unwrap().to_owned(),
            token: info["token"].as_str().unwrap().to_owned(),
            http: reqwest::Client::new(),
        }
    }

    async fn control(&self, action: &str, body: Value) -> Value {
        let response = self.http.post(format!("{}_control/{action}", self.base)).json(&body).send().await.unwrap();
        assert!(response.status().is_success());
        response.json().await.unwrap()
    }

    fn uploader(&self, region: &str) -> Uploader {
        let source = CredentialSource::new(&self.credentials_uri, self.token.clone()).unwrap();
        Uploader::new(reqwest::Url::parse(&self.base).unwrap(), region, source).unwrap()
    }
}

impl Drop for Emulator {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        crate::supervisor::forget_std_tracked(self.child.id());
    }
}

/// Quote each `"time"` number so huge literals survive parsing (JSON numbers
/// beyond f64 range are errors for serde_json); [`fixture_time`] reads them
/// as f64, overflowing to an infinity as IEEE 754 does.
fn quote_times(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(at) = rest.find("\"time\": ") {
        let start = at + "\"time\": ".len();
        out.push_str(&rest[..start]);
        let tail = &rest[start..];
        let end = tail.find(|c: char| !(c.is_ascii_digit() || "+-.eE".contains(c))).unwrap_or(tail.len());
        if end > 0 {
            out.push_str(&format!("{{\"$number\": \"{}\"}}", &tail[..end]));
        }
        rest = &tail[end..];
    }
    out.push_str(rest);
    out
}

/// A fixture time as the adapter's f64 seconds; anything not a number is NaN.
fn fixture_time(value: &Value) -> f64 {
    value.get("$number").and_then(Value::as_str).and_then(|n| n.parse().ok()).unwrap_or(f64::NAN)
}

fn seconds(ms: i64) -> Value {
    if ms % 1000 == 0 { json!(ms / 1000) } else { json!(ms as f64 / 1000.0) }
}

fn upload_json(group: &str, stream: &str, events: &[(i64, String)], result: &str) -> Value {
    json!({"group": group, "stream": stream, "events": events.iter().map(|(ms, m)| json!([seconds(*ms), m])).collect::<Vec<_>>(), "result": result})
}

/// The fixture's cumulative `configure` state, applied to [`Settings`].
fn configure(previous: &Value, op: &Value) -> (Value, anyhow::Result<Settings>) {
    let mut merged = previous.clone();
    for (key, value) in op.as_object().unwrap() {
        if key != "op" {
            merged[key] = value.clone();
        }
    }
    let minimum = merged.get("minimum_level").map_or(Some(Level::Info), |v| v.as_str().and_then(Level::parse));
    let settings = (|| {
        let minimum = minimum.ok_or_else(|| anyhow::anyhow!("unknown level"))?;
        let mut sources = BTreeMap::new();
        for (name, kind) in merged.get("sources").and_then(Value::as_object).cloned().unwrap_or_default() {
            let kind = kind.as_str().and_then(ComponentType::parse).ok_or_else(|| anyhow::anyhow!("unknown component type"))?;
            sources.insert(name, Source { component_type: kind, minimum });
        }
        let settings = Settings {
            region: merged.get("region").and_then(Value::as_str).unwrap_or("us-east-1").to_owned(),
            thing: merged.get("thing").and_then(Value::as_str).unwrap_or("thing").to_owned(),
            interval_seconds: merged.get("interval").map_or(Some(DEFAULT_INTERVAL_SECONDS), Value::as_f64).ok_or_else(|| anyhow::anyhow!("interval"))?,
            spool_limit: merged.get("spool_limit").map_or(Some(10_485_760), Value::as_u64).ok_or_else(|| anyhow::anyhow!("spool limit"))?,
            redact: merged.get("redact").map_or(Some(Vec::new()), |v| v.as_array().map(|a| a.iter().filter_map(|s| s.as_str().map(String::from)).collect())).ok_or_else(|| anyhow::anyhow!("redact"))?,
            sources,
        };
        settings.validate()?;
        Ok(settings)
    })();
    (merged, settings)
}

#[tokio::test]
async fn declared_fixtures_run_against_the_adapter_and_emulator() {
    let text = std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("../interfaces/compat/cloudwatch-logs-cases.json")).unwrap();
    let cases: Value = serde_json::from_str(&quote_times(&text)).unwrap();
    let emulator = Emulator::start();
    let (mut steps, mut decoded) = (0, 0);
    for case in cases.as_array().unwrap() {
        let id = case["id"].as_str().unwrap();
        let dir = fresh(&format!("fixture-{id}"));
        emulator.control("reset", json!({"strict": true})).await;
        let mut config = json!({});
        let mut current: Option<(Settings, Exporter, Uploader)> = None;
        let mut seen_calls = 0;
        for step in case["steps"].as_array().unwrap() {
            steps += 1;
            let op = &step["op"];
            let state = |c: &Option<(Settings, Exporter, Uploader)>| {
                let exporter = &c.as_ref().unwrap().1;
                (exporter.spool.len(), exporter.spool.dropped)
            };
            let got = match op["op"].as_str().unwrap() {
                "configure" => {
                    let (merged, result) = configure(&config, op);
                    match result {
                        Ok(settings) => {
                            config = merged;
                            drop(current.take()); // close the spool before reopening it
                            let exporter = Exporter::open(&dir, settings.clone()).unwrap();
                            let uploader = emulator.uploader(&settings.region);
                            current = Some((settings, exporter, uploader));
                            json!({"status": "ok"})
                        }
                        Err(_) => json!({"status": "invalid-config"}),
                    }
                }
                "upstream" => {
                    emulator.control("results", json!({"results": op["results"]})).await;
                    json!({})
                }
                "restart" => {
                    let (settings, exporter, _) = current.take().unwrap();
                    drop(exporter);
                    let exporter = Exporter::open(&dir, settings.clone()).unwrap();
                    current = Some((settings.clone(), exporter, emulator.uploader(&settings.region)));
                    let (spooled, dropped) = state(&current);
                    json!({"spooled": spooled, "dropped": dropped})
                }
                "log" => {
                    let exporter = &mut current.as_mut().unwrap().1;
                    let status = match op["message"].as_str() {
                        Some(message) => {
                            let message = message.repeat(op.get("repeat").and_then(Value::as_u64).unwrap_or(1) as usize);
                            exporter.log(op["source"].as_str().unwrap(), op["level"].as_str().unwrap_or(""), &message, fixture_time(&op["time"])).unwrap()
                        }
                        // The adapter's interface takes text: decoding refuses other JSON types.
                        None => {
                            decoded += 1;
                            LogStatus::Invalid
                        }
                    };
                    let (spooled, dropped) = state(&current);
                    match status {
                        LogStatus::UnknownSource => json!({"status": "unknown-source"}),
                        LogStatus::Invalid => json!({"status": "invalid"}),
                        LogStatus::Filtered => json!({"status": "filtered"}),
                        LogStatus::Spooled { events } => json!({"status": "spooled", "events": events, "spooled": spooled, "dropped": dropped}),
                        LogStatus::Dropped { events } => json!({"status": "dropped", "events": events, "spooled": spooled, "dropped": dropped}),
                    }
                }
                "tick" => {
                    let now = op["now"].as_u64().unwrap() * 1000;
                    let (_, exporter, uploader) = current.as_mut().unwrap();
                    let uploads = exporter.tick(now, uploader).await.unwrap();
                    let reported: Vec<Value> = uploads.iter().map(|u| upload_json(&u.group, &u.stream, &u.events, u.outcome.label())).collect();
                    // What the emulator received must be exactly what the adapter reports.
                    let observed = emulator.control("state", json!({})).await;
                    let calls = observed["calls"].as_array().unwrap();
                    let received: Vec<Value> = calls[seen_calls..]
                        .iter()
                        .map(|c| {
                            let events: Vec<(i64, String)> = c["events"].as_array().unwrap().iter().map(|e| (e[0].as_i64().unwrap(), e[1].as_str().unwrap().to_owned())).collect();
                            upload_json(c["group"].as_str().unwrap(), c["stream"].as_str().unwrap(), &events, c["result"].as_str().unwrap())
                        })
                        .collect();
                    seen_calls = calls.len();
                    assert_eq!(received, reported, "{id}: emulator and adapter disagree");
                    let (spooled, dropped) = state(&current);
                    json!({"uploads": reported, "spooled": spooled, "dropped": dropped})
                }
                other => panic!("unknown fixture op {other}"),
            };
            assert_eq!(got, step["expect"], "{id}: step {op}");
        }
        let observed = emulator.control("state", json!({})).await;
        assert_eq!(observed["violations"], json!([]), "{id}: request checks failed");
        assert_eq!(observed["pending_results"], json!([]), "{id}: queued results were not used");
        drop(current);
        let _ = std::fs::remove_dir_all(&dir);
    }
    assert_eq!((cases.as_array().unwrap().len(), steps), (8, 69), "every declared sequence and step ran");
    assert_eq!(decoded, 1, "only the non-text message is decided while decoding the fixture");
}

#[tokio::test]
async fn uploads_create_streams_settle_rejections_and_keep_failures() {
    let emulator = Emulator::start();
    emulator.control("reset", json!({"strict": true})).await;
    let d = fresh("outcomes");
    let mut s = settings(&[("app", ComponentType::UserComponent, Level::Info)]);
    s.interval_seconds = 1.0;
    let mut exporter = Exporter::open(&d, s).unwrap();
    let mut uploader = emulator.uploader("us-east-1");
    for i in 0..3 {
        exporter.log("app", "INFO", &format!("line {i}"), 1_759_968_000.0 + i as f64).unwrap();
    }
    // Partly refused as too old: settled, the refused event counted.
    emulator.control("results", json!({"results": ["too-old"]})).await;
    let uploads = exporter.tick(1_000, &mut uploader).await.unwrap();
    assert_eq!(uploads[0].outcome, Outcome::Ok { rejected: 1 });
    assert_eq!((exporter.spool.len(), exporter.spool.rejected), (0, 1));
    // Invalid: settled as rejected, never resent.
    exporter.log("app", "INFO", "bad", 1_759_968_010.0).unwrap();
    emulator.control("results", json!({"results": ["rejected"]})).await;
    let uploads = exporter.tick(2_000, &mut uploader).await.unwrap();
    assert!(matches!(&uploads[0].outcome, Outcome::Rejected(kind) if kind == "InvalidParameterException"));
    assert_eq!((exporter.spool.len(), exporter.spool.rejected), (0, 2));
    // Unreachable endpoint: a connection error keeps the events.
    exporter.log("app", "INFO", "kept", 1_759_968_020.0).unwrap();
    let closed = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = closed.local_addr().unwrap().port();
    drop(closed);
    let source = CredentialSource::new(&emulator.credentials_uri, emulator.token.clone()).unwrap();
    let mut unreachable = Uploader::new(reqwest::Url::parse(&format!("http://127.0.0.1:{port}/")).unwrap(), "us-east-1", source).unwrap();
    unreachable.cached = uploader.cached.clone();
    let uploads = exporter.tick(3_000, &mut unreachable).await.unwrap();
    assert_eq!(uploads[0].outcome, Outcome::ConnectionError);
    // Wrong credential token, nothing cached: no request is signed or sent.
    let wrong = CredentialSource::new(&emulator.credentials_uri, "wrong".into()).unwrap();
    let mut refused = Uploader::new(reqwest::Url::parse(&emulator.base).unwrap(), "us-east-1", wrong).unwrap();
    let uploads = exporter.tick(4_000, &mut refused).await.unwrap();
    assert!(matches!(&uploads[0].outcome, Outcome::Failed(reason) if reason.contains("401")), "{:?}", uploads[0].outcome);
    assert_eq!(exporter.spool.len(), 1);
    emulator.control("results", json!({"results": ["ok"]})).await;
    exporter.tick(5_000, &mut uploader).await.unwrap();
    assert!(exporter.spool.is_empty());
    let observed = emulator.control("state", json!({})).await;
    assert_eq!(observed["violations"], json!([]));
    assert_eq!(observed["credential_fetches"], json!(1), "credentials are cached until near expiry");
    let stored = &observed["streams"]["/aws/greengrass/UserComponent/us-east-1/app|/2025/10/09/thing/thing"];
    assert_eq!(stored.as_array().unwrap().iter().map(|e| e[1].as_str().unwrap()).collect::<Vec<_>>(), ["line 1", "line 2", "kept"]);
}
