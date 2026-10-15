# Automated security checks

Normal `CI` runs security checks on every pull request and main update, with a weekly scheduled run to detect newly disclosed advisories. A webpage-only change receives the same checks. The jobs use a read-only repository token, no GitHub environment, no wallet/release credentials and no deployment or testnet transactions. Forks use ordinary `pull_request`, never a privileged `pull_request_target` event.

## Coverage and enforcement

- **CodeQL:** the official action is pinned to commit `6f2ac81f5cd81fe351a197ba0b00d91d03cd2aee`. Its `security-extended` suite analyzes JavaScript/TypeScript, Python, Rust and GitHub Actions. Rust uses the existing Rust 1.94.0 baseline and build mode `none`.
- **Dependencies, secrets and configuration:** Trivy 0.76.0 is downloaded from its official release and its archive must match the committed SHA-256 before extraction. No mutable scanner action, installer script or action tag executes. The filesystem scan includes development dependencies supported by the scanner, both Rust lockfiles, the frontend lockfile, source secrets, both Dockerfiles and supported deployment configuration. Generated dependency/build caches are excluded.
- **Gates:** HIGH/CRITICAL and unknown-severity Trivy findings block CI. CodeQL security scores of 7 or more and SARIF error-level findings block CI. Other findings remain in the sanitized report. Scanner/download errors, missing reports, failed analysis invocations, malformed rule references, and missing required lockfile/Dockerfile coverage also fail CI.

The scanner runs with an explicitly empty configuration and no ignore file. Existing findings are not silently baselined away. Exceptions and suppressed SARIF results do not bypass the local gate. CodeQL successful execution alone is insufficient: `scripts/security_audit.py` reads the actual results.

All reported CI jobs must pass before the PR coordinator can merge. The automatic testnet controller still follows successful originating CI, so these jobs do not broaden access to its protected wallet. Production publication is not authorized by a security scan.

## Reports and local reproduction

Raw scanner JSON/SARIF can contain source excerpts or secret matches. Keep it in a private temporary directory outside the checkout; never attach it to issues, upload it as an artifact or print it. CI uploads only sanitized JSON containing finding identifiers, severity and safe repository-relative paths. CodeQL SARIF upload is disabled; this implementation supplies CI gates/artifacts, not GitHub Security-tab alerts.

```bash
umask 077
security_tmp=$(mktemp -d)
python3 scripts/install_security_scanner.py "$security_tmp/trivy"
printf '{}\n' > "$security_tmp/empty.yaml"
"$security_tmp/trivy" filesystem \
  --config "$security_tmp/empty.yaml" --secret-config "$security_tmp/empty.yaml" \
  --ignorefile /dev/null --include-dev-deps --scanners vuln,secret,misconfig \
  --skip-dirs '.git,**/node_modules,**/.next,**/target,.ci-cache' \
  --format json --output "$security_tmp/private.json" --quiet .
python3 scripts/security_audit.py trivy "$security_tmp/private.json" \
  --output security-reports/dependencies.json
python3 -m unittest discover -s scripts/tests -p test_security_audit.py -v
```

The installer currently targets Linux x86-64, matching the GitHub runner. Scanner databases and findings evolve; the binary checksum does not freeze the advisory database. Download/database failures are errors, not clean scans. For local CodeQL analysis, use its generated SARIF files with `python3 scripts/security_audit.py sarif FILE... --output security-reports/codeql.json`; do not substitute fabricated reports for real acceptance.

## Findings and limits

The initial real filesystem scan reported **2 critical and 17 high frontend dependency findings**, plus lower-severity findings, on the pre-remediation lockfile. SF-049/PR #81 resolved those findings with reviewed dependency changes; see [the advisory inventory and compatibility limits](FRONTEND_SECURITY.md). A fresh checksum-verified Trivy 0.76.0 scan after integrating that remediation on 2026-10-15 reported **6 findings, zero blocking**. No findings were suppressed. Package advisories require applicability review: a reported server-side vulnerability does not establish exploitability of a static browser deployment.

The remaining findings stay visible in sanitized artifacts and [SF-048/#78](https://github.com/tyler-suifleet/suifleet/issues/78):

| Advisory/rule | Severity | Committed target |
|---|---|---|
| CVE-2026-69153 (PostCSS) | MEDIUM | frontend lockfile |
| GHSA-2mjx-qc3c-rqvc (rustls) | MEDIUM | both Rust lockfiles (two records) |
| CVE-2026-45784 (OpenSSL) | MEDIUM | indexer lockfile |
| DS-0026 (missing healthcheck) | LOW | both Dockerfiles (two records) |

Real Actions CodeQL analysis initially exposed four critical checkout TOCTOU results in the automatic testnet workflow. The controller now checks out its immutable workflow commit, selects test code by immutable event commit, and verifies actual Git HEAD against the independent server-authenticated planner selection before building or using credentials. Existing same-repository writer/actor, successful originating CI, environment and pre-spend checks remain. All four CodeQL languages subsequently reported zero findings on that repair; fresh current-head CI remains a merge gate. SARIF rule metadata is resolved through the referenced driver or extension component, with identity and bounds checks; taxonomy extensions may legitimately omit rules.

Fix vulnerable dependencies with manifest/lockfile changes and relevant regression/build evidence, then rerun the real scan. Track unpatched advisories and source findings explicitly. New suppressions or accepted risks require reviewed, specific rationale; no broad ignore file is configured here.

These checks do not prove AWS Greengrass parity, Move-contract authority safety, real wallet/device security, complete application security, or absence of vulnerabilities. Filesystem scanning does not inspect the installed OS packages in a built dev/release image or a final Yocto image. Existing Move tests, upgrade attack tests, effects checks, wallet acceptance and Pi testing remain required for their respective claims.

Sources: [CodeQL language support](https://codeql.github.com/docs/codeql-overview/supported-languages-and-frameworks/), [CodeQL query suites](https://docs.github.com/en/code-security/reference/code-scanning/codeql/codeql-queries), [SARIF 2.1.0 sections 3.19.23, 3.52 and 3.54](https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/sarif-v2.1.0-os.html), [CodeQL checkout TOCTOU guidance](https://codeql.github.com/codeql-query-help/actions/actions-untrusted-checkout-toctou-critical/), [Trivy filesystem scanning](https://trivy.dev/docs/dev/target/filesystem/), and [Trivy secret scanning](https://www.trivy.dev/docs/latest/guide/scanner/secret/).
