//! Log redaction (SF-020), shared by the daemon (before a line leaves the
//! device) and the indexer (again on every search and diagnostic bundle).
//! Dependency-free, so both crates compile this one file; both are tested on
//! `interfaces/log-redaction-cases.json`.
//!
//! Redacted, replacing the secret (never quoting it):
//! - Sui private keys (`suiprivkey1…`) and PEM private-key lines;
//! - values after secret-like names (`password=`, `token: `, `api_key=`,
//!   `secret`, `pin`, `mnemonic`, `seed`, `private_key`, `credential`,
//!   `authorization`), quoted or not, and `Bearer` tokens;
//! - 64 or more hex digits not written as a `0x` object ID or address (raw
//!   keys), and base64 runs of 40 or more characters that carry `+`, `/` or
//!   `=` (encoded keys; base58 digests and base64url blob IDs are kept);
//! - runs of 12 or more short lowercase words (recovery phrases);
//! - the device's exact confidential values (configuration overlays).
//!
//! The rules prefer over-redaction. They are a safety net, not a licence to
//! log secrets: components and the daemon must still not write them.

/// What replaces a secret.
pub const MARK: &str = "[REDACTED]";

const SECRET_NAMES: &[&str] = &[
    "password", "passwd", "passphrase", "pwd", "secret", "token", "apikey", "api_key", "api-key", "accesskey",
    "access_key", "access-key", "authorization", "auth", "pin", "mnemonic", "seed", "privatekey", "private_key",
    "private-key", "credential", "credentials",
];

/// Redact one line; returns it and how many secrets were replaced.
pub fn redact(line: &str, secrets: &[String]) -> (String, usize) {
    let mut count = 0;
    let mut text = line.to_owned();
    if text.contains("PRIVATE KEY") && text.contains("-----") {
        return (format!("{MARK} (private key block line)"), 1);
    }
    for secret in secrets {
        if secret.chars().count() >= 4 && text.contains(secret.as_str()) {
            count += text.matches(secret.as_str()).count();
            text = text.replace(secret.as_str(), MARK);
        }
    }
    let (text, n) = sui_private_keys(&text);
    count += n;
    let (text, n) = named_values(&text);
    count += n;
    let (text, n) = bearer(&text);
    count += n;
    let (text, n) = long_runs(&text);
    count += n;
    let (text, n) = phrases(&text);
    count += n;
    (text, count)
}

fn sui_private_keys(text: &str) -> (String, usize) {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    let mut count = 0;
    while let Some(at) = rest.find("suiprivkey1") {
        out.push_str(&rest[..at]);
        let tail = &rest[at + "suiprivkey1".len()..];
        let end = tail.find(|c: char| !c.is_ascii_alphanumeric()).unwrap_or(tail.len());
        out.push_str(MARK);
        count += 1;
        rest = &tail[end..];
    }
    out.push_str(rest);
    (out, count)
}

fn is_word_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_' || c == '-'
}

/// `name = value`, `name: value`, `"name": "value"`.
fn named_values(text: &str) -> (String, usize) {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut count = 0;
    let mut i = 0;
    while i < chars.len() {
        // A word starts here?
        if is_word_char(chars[i]) && (i == 0 || !is_word_char(chars[i - 1])) {
            let start = i;
            while i < chars.len() && is_word_char(chars[i]) {
                i += 1;
            }
            let word: String = chars[start..i].iter().collect::<String>().to_ascii_lowercase();
            let word = word.trim_end_matches('s');
            let named = SECRET_NAMES.iter().any(|n| n.trim_end_matches('s') == word)
                || SECRET_NAMES.iter().any(|n| word.ends_with(&format!("_{n}")) || word.ends_with(&format!("-{n}")));
            out.extend(&chars[start..i]);
            if !named {
                continue;
            }
            // Optional closing quote of a JSON key, spaces, then : or =.
            let mut j = i;
            if j < chars.len() && (chars[j] == '"' || chars[j] == '\'') {
                j += 1;
            }
            while j < chars.len() && chars[j] == ' ' {
                j += 1;
            }
            if j >= chars.len() || (chars[j] != ':' && chars[j] != '=') {
                continue;
            }
            j += 1;
            while j < chars.len() && chars[j] == ' ' {
                j += 1;
            }
            if j >= chars.len() {
                continue;
            }
            // `Authorization: Bearer TOKEN` / `Basic TOKEN`: keep the scheme.
            for scheme in ["bearer ", "basic "] {
                let ahead: String = chars[j..chars.len().min(j + scheme.len())].iter().collect::<String>().to_ascii_lowercase();
                if ahead == scheme {
                    j += scheme.len();
                }
            }
            out.extend(&chars[i..j]);
            // The value: quoted, or up to whitespace or a separator.
            let mut k = j;
            if chars[k] == '"' || chars[k] == '\'' {
                let quote = chars[k];
                k += 1;
                while k < chars.len() && chars[k] != quote {
                    k += 1;
                }
                out.push(quote);
                out.push_str(MARK);
                if k < chars.len() {
                    out.push(quote);
                    k += 1;
                }
            } else {
                while k < chars.len() && !chars[k].is_whitespace() && !matches!(chars[k], ',' | ';' | '&' | '}' | ')') {
                    k += 1;
                }
                if k == j {
                    i = j;
                    continue;
                }
                out.push_str(MARK);
            }
            count += 1;
            i = k;
            continue;
        }
        out.push(chars[i]);
        i += 1;
    }
    (out, count)
}

fn bearer(text: &str) -> (String, usize) {
    let lower = text.to_ascii_lowercase();
    let mut out = String::with_capacity(text.len());
    let mut last = 0;
    let mut count = 0;
    let mut search = 0;
    while let Some(found) = lower[search..].find("bearer ") {
        let at = search + found;
        let value_start = at + "bearer ".len();
        let value_end = text[value_start..]
            .find(|c: char| c.is_whitespace() || c == '"' || c == '\'')
            .map(|e| value_start + e)
            .unwrap_or(text.len());
        if value_end - value_start >= 8 && !text[value_start..value_end].starts_with(MARK) {
            out.push_str(&text[last..value_start]);
            out.push_str(MARK);
            last = value_end;
            count += 1;
        }
        search = value_end.max(value_start);
    }
    out.push_str(&text[last..]);
    (out, count)
}

/// Raw hex keys (64+ hex digits not after `0x`) and padded/standard base64
/// runs of 40+ characters.
fn long_runs(text: &str) -> (String, usize) {
    let bytes = text.as_bytes();
    let mut out = String::with_capacity(text.len());
    let mut count = 0;
    let mut i = 0;
    let mut last = 0;
    while i < bytes.len() {
        let boundary = i == 0 || !(bytes[i - 1].is_ascii_alphanumeric() || matches!(bytes[i - 1], b'+' | b'/'));
        if boundary && (bytes[i].is_ascii_alphanumeric() || matches!(bytes[i], b'+' | b'/')) {
            let start = i;
            let mut end = i;
            while end < bytes.len() && (bytes[end].is_ascii_alphanumeric() || matches!(bytes[end], b'+' | b'/')) {
                end += 1;
            }
            let mut padded = end;
            while padded < bytes.len() && padded - end < 2 && bytes[padded] == b'=' {
                padded += 1;
            }
            let run = &text[start..end];
            let after_0x = start >= 2 && &text[start - 2..start] == "0x"
                && (start == 2 || !bytes[start - 3].is_ascii_alphanumeric());
            let hex = run.len() >= 64 && run.bytes().all(|b| b.is_ascii_hexdigit()) && !after_0x;
            let base64 = end - start >= 40
                && (run.contains('+') || run.contains('/') || padded > end)
                && !run.bytes().all(|b| b.is_ascii_hexdigit());
            if hex || base64 {
                out.push_str(&text[last..start]);
                out.push_str(MARK);
                last = if base64 { padded } else { end };
                count += 1;
            }
            i = end.max(i + 1);
            continue;
        }
        i += 1;
    }
    out.push_str(&text[last..]);
    (out, count)
}

/// Twelve or more consecutive words of 3 to 8 lowercase letters.
fn phrases(text: &str) -> (String, usize) {
    let words: Vec<(usize, usize)> = text
        .split(' ')
        .scan(0usize, |offset, word| {
            let start = *offset;
            *offset += word.len() + 1;
            Some((start, start + word.len()))
        })
        .collect();
    let short = |(s, e): &(usize, usize)| {
        let w = &text[*s..*e];
        (3..=8).contains(&w.len()) && w.bytes().all(|b| b.is_ascii_lowercase())
    };
    let mut runs = Vec::new();
    let mut i = 0;
    while i < words.len() {
        if short(&words[i]) {
            let start = i;
            while i < words.len() && short(&words[i]) {
                i += 1;
            }
            if i - start >= 12 {
                runs.push((words[start].0, words[i - 1].1));
            }
        } else {
            i += 1;
        }
    }
    if runs.is_empty() {
        return (text.to_owned(), 0);
    }
    let mut out = String::with_capacity(text.len());
    let mut last = 0;
    for (start, end) in &runs {
        out.push_str(&text[last..*start]);
        out.push_str(MARK);
        last = *end;
    }
    out.push_str(&text[last..]);
    (out, runs.len())
}

/// Every string leaf of a JSON value (confidential overlay values).
#[allow(dead_code)] // The daemon uses it; the indexer shares this file.
pub fn string_leaves(value: &serde_json::Value, out: &mut Vec<String>) {
    match value {
        serde_json::Value::String(s) => out.push(s.clone()),
        serde_json::Value::Array(items) => items.iter().for_each(|v| string_leaves(v, out)),
        serde_json::Value::Object(map) => map.values().for_each(|v| string_leaves(v, out)),
        serde_json::Value::Number(n) => out.push(n.to_string()),
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shared_redaction_cases() {
        let cases: serde_json::Value = serde_json::from_str(include_str!("../../interfaces/log-redaction-cases.json")).unwrap();
        for case in cases.as_array().unwrap() {
            let secrets: Vec<String> = case["secrets"].as_array().map(|a| a.iter().map(|s| s.as_str().unwrap().to_owned()).collect()).unwrap_or_default();
            let (out, count) = redact(case["line"].as_str().unwrap(), &secrets);
            assert_eq!(out, case["redacted"].as_str().unwrap(), "{}", case["name"]);
            assert_eq!(count as u64, case["count"].as_u64().unwrap(), "{}", case["name"]);
        }
    }
}
