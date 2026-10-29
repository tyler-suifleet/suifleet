# Public documentation build (SF-177)

Source of truth: the explicitly listed Markdown files under docs/ in pages.json.
No directory crawl publishes tracking, agent, coordination, business plans or
validation receipts as documentation pages. Relative links to other existing
repository files become public GitHub source links; they are not copied into the
site. Missing source targets fail the build. Curated cross-links stay on the
docs domain, including their anchors. External URLs are not fetched by CI.

## Reproduce

Python 3.10+; the renderer and its sole dependency are version/hash pinned.
Install into your own virtual environment, then run from the repository root:

~~~sh
python3 -m pip install --require-hashes -r site/docs/requirements.txt
python3 -m unittest discover -s site/docs/tests -v
node site/docs/tests/search.cjs
python3 site/docs/build.py --out /tmp/suifleet-docs
python3 site/docs/build.py --check --out /tmp/suifleet-docs
~~~

The Documentation workflow executes these checks on every PR and main push,
and uploads the deployable static output plus SHA256SUMS. No credentials,
publication, DNS, chain writes or hardware operations occur. Only a previously
marked docs output can be replaced by the builder. Build output has no timestamp.

With existing dapp dependencies and local Google Chrome installed, optional
offline browser QA exercises mobile/desktop layout and search/navigation without
opening a port or making network requests:

~~~sh
node site/docs/tests/browser.cjs /tmp/suifleet-docs
~~~

## Stable public contract

Slugs in pages.json are public API, independent of source filenames and headings.
Keep them when moving source documents. To retire a slug, ship a static alias page
at the old path and check its destination before removing it. Existing console
links are checked against actual output by the regression suite. Heading anchors
are generated deterministically; keep explicit named anchors when renaming linked
headings. The build accepts only safe empty named anchors from raw HTML and
escapes other HTML.

Navigation groups are Operators, Device integrators and Administrators. Search
indexes only curated pages; search queries remain in browser memory and never
leave the browser. The only script is local search; there are no tracking scripts,
cookies, remote fonts or embedded third-party resources. CSP limits resources to
this origin. Adding analytics requires reviewed consent behavior first.

## SF-172 integration and hosting handoff

This subtree follows the committed SF-172 site interface and the SF-180 identity
merged in PR #365: the three-sail logo/favicon, shared stylesheet and self-hosted
Inter font (with its license) match site/src/assets. Font loading is restricted to
this origin in both the HTML CSP and emitted hosting headers. The logo preserves
its wide aspect ratio; documentation navigation, search and article rules live in
docs.css. Reconcile these copied assets whenever the public site identity changes.

The build remains independent: site/build.py serves suifleet.io, while this builder
produces the separate docs.suifleet.io document root. No landing-site sources or
recording work are modified.

SF-176 owns hosting, TLS and DNS. Serve the built directory with directory-index
routing and a real 404 status using 404.html; apply the emitted _headers or
equivalent host headers. Do not serve the repository root. No production
publication has occurred. Before public release, a reviewer without repository
access must read the built install guide and follow its public source links,
and the operator must verify external links and domain routing. Browser QA is
not a substitute for that independent usability review. Physical, provider,
real-wallet and mainnet acceptance remain separately tracked in #32 and #159.
