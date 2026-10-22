# SuiFleet manufacturer discovery shortlist

Research snapshot: 2026-10-21. Research only; no email, form submission, account creation, outreach or customer contact. This is a set of prospect hypotheses, not a customer pipeline. No fleet size, budget, pricing, dissatisfaction, contact address or purchase intent is asserted.

Use the [ranked report](REPORT.md) for reading and [CSV](manufacturers.csv) for reuse. The CSV is canonical; regenerate the report with `python3 scripts/check_manufacturer_prospects.py --write` and validate with `python3 scripts/check_manufacturer_prospects.py`. `make test-prospects` runs the validator and its failure-path regressions, including under optimized Python; it is included in `make check` and CI. Both commands use only Python's standard library and work offline. The report deliberately includes every record's evidence, uncertainty and migration caveat rather than only a top-ten summary.

## Scope and sourcing

Start from the [business plan](../../BUSINESS_PLAN.md): embedded Linux manufacturers, authenticated shared releases, metered deployment/report costs and customer-controlled platform integration. SuiFleet is not established production Greengrass parity. Network savings, availability and customer willingness to adopt Sui/Walrus remain hypotheses; do not pitch a proven price reduction or replace the customer's boot/recovery system.

Search manufacturer product documentation, manuals, vendor repositories and supplier-published customer interviews/case studies. Each row has a primary technical URL. Supplier case studies are primary evidence of the supplier's published relationship claim, not independent proof of a current installation. Separate OTA evidence URLs support named incumbents; a software integration guide does not establish actual fleet installation. Historical articles do not establish today's stack, corporate ownership or product availability. No gated case-study download was required.

The snapshot uses publicly retrievable/indexed source content, not private customer information. `checked_on` is the research date, not the publication date, site certification or a sales-availability check. Manuals may describe older SKUs. Availability caveats apply to every record; stronger warnings identify older/preliminary evidence. Recheck product pages, supported images, corporate ownership and ordering/support status before any future authorized discovery. Geography is intentionally unknown unless the cited material explicitly supplies it; Kuva's entry describes a market, not headquarters. Unknown is a finding, not a gap filled by inference.

Selection spans industrial gateways/controllers, energy monitoring/storage/charging, and commercial cameras/signage/printers/kitchen appliances. This is a purposive technical sample, not a representative market survey or estimate of the addressable market. Publicly documented Linux hardware is easier to find, so gateways and incumbents are overrepresented. One record per named company; brands/products are not counted as separate prospects. Parent-company consolidation must be rechecked before CRM import. The dataset contains 50 manufacturer candidates; their own end-fleet ownership is unverified.

## Roles and ranking

`manufacturer` means a branded hardware/system offering is documented; it does not establish in-house fabrication or that the vendor operates the installed fleet. `/channel` marks module/gateway suppliers whose downstream OEM or integrator may be the actual buyer. `/integrator` marks combined hardware/software system integration (Octave and Winnow). `/platform vendor` flags an existing management ecosystem and possible commercial competition. These are research classifications, not verified corporate/legal categories. Pure integrators/platforms with unverified manufacturing roles, such as Raydiant, are excluded from the 50; the documented balena signage case is a possible future channel reference, not a manufacturer counted here.

Scores are ordinal research judgments, reproducible from three CSV columns:

| Input | 0 | 1 | 2 |
| --- | --- | --- | --- |
| Linux evidence (L) | No supported Linux host established | Container/embedded app or connected OS evidence; exact supported Linux host unconfirmed | Explicit Linux/Yocto/Debian/Ubuntu/JetPack evidence for the cited platform or historical deployment |
| Application boundary (A) | No credible supported independent application boundary established | Possible ancillary application; runtime/safety boundary needs discovery | Programmable gateway, module or application workload suggests a separable lab application |
| Friction (F) | No identified friction (not used in this sample) | BSP/support and downstream ownership questions | Named management ecosystem, safety/control boundary or substantial enterprise integration |

**Score = 2L + A − F** (possible −2 to 6). Descending score, then case-insensitive company name for ties. A score is not a probability, revenue estimate, readiness certification or evidence of unmet need. An unknown incumbent is not a competitive advantage. No fleet size or company scale enters the formula. Enterprise procurement friction is inferred from the product/ecosystem, not a claim about an observed sales cycle. Even high-scoring industrial devices may be deployed in safety-sensitive systems: only non-control workloads belong in an initial pilot.

## Prioritized discovery plan

No discovery calls are authorized by this ticket. The following order prepares a future human-approved research/contact phase.

1. **Reference-integration discovery:** Advantech, Kontron, InHand Networks, Compulab, KUNBUS, Sfera Labs, SolidRun, Seeed Studio and OnLogic. Their programmable gateway evidence supports a small non-control lab deployment. First determine whether the manufacturer or downstream OEM owns the software and fleet. These are potential channels as well as potential buyers; do not count downstream fleets twice.
2. **Platform-preserving integration discovery:** PHYTEC, Variscite, SYS TEC electronic and embeddedTS. Establish exact architecture/BSP and current supported image. Keep RAUC/SWUpdate/boot control with the platform owner. A module reference integration is not a customer fleet pilot.
3. **Direct workload discovery:** Kuva, Octave and Winnow. Their published application/OTA evidence makes a concrete release-workload comparison possible, but established Mender/balena integrations create migration friction. Ask whether a parallel, non-production application trial is useful; never assume dissatisfaction or demand. Historical stack details require reconfirmation.
4. **Longer-cycle/conditional candidates:** Axis, Eurotech, Hilscher, Toradex and the remaining controllers, charging/storage, printing and cooking vendors. Preserve OEM firmware and safety boundaries. Require a supported app runtime and named integration owner before scheduling a technical pilot. Low scores indicate weak evidence for the present Linux-agent boundary, not poor businesses.

The CSV's numerical rank is technical fit; this staged plan distinguishes channel feasibility, direct workload learning and integration burden. Neither is a commitment to build a custom connector or support a new architecture.

## Discovery questions, in priority order

| Priority | Question | Decision/evidence sought |
| --- | --- | --- |
| P0 | Who owns release authority, hardware signing keys, installed software and customer contracts: manufacturer, integrator or fleet operator? | Identify technical sponsor and economic buyer roles; no named contacts assumed. |
| P0 | What exact CPU/ABI, OS/BSP, service manager, free disk/RAM and supported third-party runtime exist? | Qualify the existing Rust/Linux agent; reject unsupported MCU/closed-runtime assumptions. |
| P0 | Which workload can update independently without changing routing, actuation, battery protection, charging, motion or boot behavior? | Written non-safety application boundary and OEM approval. |
| P0 | What updater/provider is installed per product generation, and what signing, partition, rollback and support constraints must remain? | Real migration/coexistence scope; platform owns reboot and firmware recovery. |
| P1 | How many devices share each artifact, how often do releases occur, what are artifact/recipe sizes, retention, regions and connectivity constraints? | Customer-provided workload; do not infer fleet size from site counts or shipments. |
| P1 | What are current paid platform/storage/RPC/egress/provider costs and contractual switching constraints? | Comparable complete bill with consent; no savings claim from partial network fees. |
| P1 | What confidentiality, residency, offline/cache, revocation and key-provider requirements apply? | Determine whether encrypted delivery and public authority/receipt metadata are acceptable. Never upload customer secrets to chain. |
| P1 | What maintenance windows, failure/restart tests and device-reported health evidence would make a lab trial useful? | Agreed pass/fail criteria; a signed report does not prove healthy physical boot. |
| P1 | Who approves security, procurement, warranty, product compliance and support, and is a change budget actually available? | Explicit buyer interest and budget remain unknown until supplied. |
| P2 | Would an optional usage-priced service or customer-operated stack be preferable, and what objections exist to Sui/Walrus? | Test the commercial thesis without assuming blockchain demand. |

A proposed first pilot uses a disposable customer-approved lab gateway, one non-control component, authenticated release/recipe bytes, an unchanged platform updater and a customer-provided workload. Measure complete costs, delivery/retry behavior, authority/revocation and state preservation. Define success with the participant; no pilot has occurred or been sold here.

## Validation and acceptance limits

The validator checks 50 names, required nonempty fields, all three sectors, structural primary HTTPS coverage, explicit commercial unknowns, allowed scoring inputs, deterministic ranks and exact CSV/report consistency. Structural URL validation is not a live-link, technical compatibility or business qualification test. Research source links support the adjacent facts; risk and pilot fields are explicitly inferences. No physical device, production provider account, manual real-wallet or mainnet testing was performed for this research. Final product acceptance remains in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159). Existing UI confusion reported by the user remains evidence of an unfinished customer flow; this research does not resolve or relabel that live acceptance.

[Source retrieval metadata](source-checks.json) covers all 63 distinct cited URLs: 57 returned HTTP 200, four returned 403, and two hosts were unreachable from the research process. OnLogic, SolidRun and Weidmuller denied automated access; Agrointelli and SYS TEC were unreachable. Their facts rely on the public search-indexed vendor content retrieved during research, not a successful direct page fetch. Removed stale Hilscher/Victron links were replaced with current accessible vendor references. The metadata stores status/redirect targets only, not copied pages. A successful response is not a stock check, an independent technical verification or confirmation that a historical OTA relationship continues today.
