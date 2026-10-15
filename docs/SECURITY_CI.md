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

The initial real filesystem scan reported **2 critical and 17 high frontend dependency findings**, plus lower-severity findings, on the pre-remediation lockfile. They include Next.js 14.2.35 and transitive build dependencies. SF-049 tracks remediation before a green security-gate rollout; SF-048 tracks this audit implementation. Do not claim the audit passes or waive these findings to merge it. Package advisories require applicability review: a reported server-side vulnerability does not establish exploitability of a static browser deployment.

Fix vulnerable dependencies with manifest/lockfile changes and relevant regression/build evidence, then rerun the real scan. Track unpatched advisories and source findings explicitly. New suppressions or accepted risks require reviewed, specific rationale; no broad ignore file is configured here.

These checks do not prove AWS Greengrass parity, Move-contract authority safety, real wallet/device security, complete application security, or absence of vulnerabilities. Filesystem scanning does not inspect the installed OS packages in a built dev/release image or a final Yocto image. Existing Move tests, upgrade attack tests, effects checks, wallet acceptance and Pi testing remain required for their respective claims.

Sources: [CodeQL language support](https://codeql.github.com/docs/codeql-overview/supported-languages-and-frameworks/), [CodeQL query suites](https://docs.github.com/en/code-security/reference/code-scanning/codeql/codeql-queries), [Trivy filesystem scanning](https://trivy.dev/docs/dev/target/filesystem/), and [Trivy secret scanning](https://www.trivy.dev/docs/latest/guide/scanner/secret/).
