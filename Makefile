export UID := $(shell id -u)
export GID := $(shell id -g)

# Detect whether we are already inside the dev container.
# If so, run commands directly; otherwise shell out via docker compose exec.
ifdef INSIDE_DEV_CONTAINER
RUN :=
else
RUN := docker compose exec --user dev dev
endif

.PHONY: dev shell down build-contracts test-contracts publish-contracts \
        dapp-install dapp-dev dapp-build edge-build edge-test

dev:
	docker compose up -d --build dev

shell:
	docker compose exec --user dev dev bash

# Tear down containers AND remove volumes (fixes stale root-owned volume data).
down:
	docker compose down -v

build-contracts:
	$(RUN) sui move build --path contracts

test-contracts:
	$(RUN) sui move test --path contracts

# Spends gas: PUBLISH_ARGS="--expect-address 0x... [--dry-run]" (docs/DEPLOYMENT_MANIFEST.md).
publish-contracts:
	$(RUN) bash scripts/redeploy.sh $(PUBLISH_ARGS)

dapp-install:
	$(RUN) bash -c "cd dapp && pnpm install --frozen-lockfile"

dapp-dev:
	$(RUN) bash -c "fuser -k 3000/tcp 2>/dev/null; sleep 1; cd dapp && pnpm dev --hostname 0.0.0.0"

dapp-build:
	@if ls deployments/*/activating.json >/dev/null 2>&1; then \
		echo "deployments: an interrupted activation left $$(ls deployments/*/activating.json); run scripts/redeploy.sh --reconcile to finish it" >&2; exit 1; fi
	$(RUN) bash -c "cd dapp && pnpm build"

edge-build:
	$(RUN) bash -c "cd suifleet-edge && cargo build --locked --release"

edge-test:
	$(RUN) bash -c "cd suifleet-edge && cargo test --locked"

.PHONY: test-ota test-component dapp-typecheck

# The daemon speaks gRPC; the simulator is served through the test-only shim
# example (suifleet-edge/examples/grpc_sim_shim.rs).
test-ota:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
	$(RUN) python3 scripts/test_ota.py
	$(RUN) python3 scripts/test_ota.py --firmware-helper
	$(RUN) python3 scripts/test_firmware_restart.py

# SF-030 harmless test component: fixture tests (system shell, plus busybox when
# installed), then the real daemon applying it through suifleet-test-updater.
test-component:
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_test_component.py'
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_component_ota.py

dapp-typecheck:
	$(MAKE) test-testnet
	$(MAKE) test-manifest
	$(RUN) bash -c "cd dapp && pnpm typecheck"

.PHONY: check check-interfaces generate-interfaces test-protocol-live test-interfaces toolchain-versions ci tracking-plan test-tracking

# Compare full reviewed ABI/layouts and generated clients with compiled contracts.
check-interfaces:
	$(RUN) python3 scripts/check_contract_interfaces.py
	$(RUN) python3 scripts/generate_interfaces.py --check
	$(RUN) python3 scripts/test_generated_interfaces.py
	$(RUN) node scripts/test_protocol.cjs

generate-interfaces:
	$(RUN) python3 scripts/generate_interfaces.py

# Disposable localnet only; run inside a container with no public-network access.
test-protocol-live:
	$(RUN) python3 scripts/test_protocol_localnet.py

# Offline regression tests for the drift gate itself (synthetic summaries).
test-interfaces:
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_check_contract_interfaces.py'

toolchain-versions:
	@$(RUN) bash -c 'echo "sui: $$(sui --version)"; echo "rustc: $$(rustc --version)"; \
		echo "cargo: $$(cargo --version)"; echo "node: $$(node --version)"; echo "pnpm: $$(pnpm --version)"; \
		echo "python3: $$(python3 --version 2>&1)"'

# CI entry point (run inside the dev image): each step's output is kept under
# ci-logs/ for failure diagnostics. No step publishes, spends gas or uses secrets.
ci: SHELL := /bin/bash
CI_STEPS := test-testnet test-publish test-contracts test-interfaces check-interfaces edge-test test-ota test-component dapp-typecheck dapp-build
ci:
	@mkdir -p ci-logs
	@$(MAKE) --no-print-directory toolchain-versions | tee ci-logs/toolchain-versions.txt
	@set -o pipefail; for step in dapp-install $(CI_STEPS); do \
		echo "::group::$$step"; \
		$(MAKE) --no-print-directory $$step 2>&1 | tee ci-logs/$$step.log || { echo "::endgroup::"; echo "::error::$$step failed (see ci-logs/$$step.log)"; exit 1; }; \
		echo "::endgroup::"; \
	done
	git diff --check

# Sequential recipe: cross-layer checks must not race over shared build artifacts.
check:
	$(MAKE) test-manifest
	$(MAKE) test-publish
	$(MAKE) test-contracts
	$(MAKE) test-interfaces
	$(MAKE) check-interfaces
	$(MAKE) edge-test
	$(MAKE) test-ota
	$(MAKE) test-component
	$(MAKE) dapp-typecheck
	$(MAKE) dapp-build
	git diff --check

# Offline: publisher metadata parsing, verification and failure handling.
.PHONY: test-publish
test-publish:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_publish.py'

# Signed releases (docs/RELEASES.md). These run on the host with Docker, not
# in the dev container: the release builder is its own pinned image.
.PHONY: release-image release-artifacts release-verify
RELEASE_IMAGE ?= suifleet-release:local
RELEASE_OUT ?= dist

release-image:
	docker build -f docker/release.Dockerfile --build-arg USER_UID=$(UID) --build-arg USER_GID=$(GID) -t $(RELEASE_IMAGE) docker/

# Builds the committed HEAD twice and fails unless the bundles are identical.
release-artifacts: release-image
	python3 scripts/release.py build --out $(RELEASE_OUT) --image $(RELEASE_IMAGE) --reproduce

# make release-verify BUNDLE=dist TRUST=release/trust.toml
release-verify:
	$(RUN) bash -c "cd suifleet-edge && cargo run --locked --quiet --bin suifleet -- --verify-release ../$(BUNDLE) ../$(TRUST)"

tracking-plan:
	python3 scripts/bootstrap_tracking.py

test-tracking:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_tracking.py'

.PHONY: test-manifest test-manifest-live
test-manifest:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_deployment_manifest.py'
	$(RUN) node scripts/test_deployment_manifest.cjs
	$(RUN) node scripts/test_wallet_manifest.cjs
	$(RUN) node scripts/test_deployment_receipts.cjs
	$(RUN) node scripts/test_frontend_queries.cjs
	$(RUN) node scripts/test_target_groups.cjs

test-manifest-live:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example chain_facts
	$(RUN) python3 scripts/test_manifest_upgrades.py

# SF-006 publisher against a disposable localnet (start it first): refusals, dry
# run, fresh publish, upgrade, incompatible upgrade; both clients verify.
.PHONY: test-publish-localnet
test-publish-localnet:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example chain_facts
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_publish_localnet.py

# Live gate (spends testnet gas; docs/TESTNET.md): base contracts published,
# upgraded in place to this tree, real daemon, on-chain receipts.
# SUIFLEET_WALRUS=testnet stores recipes/artifacts in real Walrus testnet.
#   make live-gate-package-upgrade SUIFLEET_WALLET=~/.config/suifleet/testnet/agent/client.yaml
.PHONY: live-gate-package-upgrade live-gate-upgrade-security live-gate-wallet
live-gate-package-upgrade:
	@test -n "$(SUIFLEET_WALLET)" || { echo "set SUIFLEET_WALLET to a funded wallet's client.yaml"; exit 1; }
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example chain_facts
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 scripts/live_package_upgrade.py --wallet "$(SUIFLEET_WALLET)" \
		--network "$${SUIFLEET_NETWORK:-testnet}" $(if $(BASE_REF),--base-ref "$(BASE_REF)") \
		$(if $(SUIFLEET_STATE),--state "$(SUIFLEET_STATE)") $(if $(SUIFLEET_WALRUS),--walrus "$(SUIFLEET_WALRUS)") \
		--evidence "$${SUIFLEET_EVIDENCE:-live-evidence.json}"

# Old-package/legacy-object attacks across a real upgrade (~0.22 SUI; per release).
live-gate-upgrade-security:
	@test -n "$(SUIFLEET_WALLET)" || { echo "set SUIFLEET_WALLET to a funded wallet's client.yaml"; exit 1; }
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example chain_facts
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 scripts/live_upgrade_security.py --wallet "$(SUIFLEET_WALLET)" \
		--network "$${SUIFLEET_NETWORK:-testnet}" --evidence "$${SUIFLEET_EVIDENCE:-live-security-evidence.json}"

# The dApp in headless Chromium against testnet (needs dapp deps and Playwright
# Chromium; reuses the package-upgrade fixture, so run that gate first).
live-gate-wallet:
	@test -n "$(SUIFLEET_WALLET)" || { echo "set SUIFLEET_WALLET to a funded wallet's client.yaml"; exit 1; }
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example chain_facts
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 scripts/live_wallet_gate.py --wallet "$(SUIFLEET_WALLET)" \
		$(if $(SUIFLEET_STATE),--state "$(SUIFLEET_STATE)") --evidence "$${SUIFLEET_EVIDENCE:-live-wallet-evidence.json}"

# Real daemon over gRPC against a disposable localnet (start it first, as for
# test-manifest-live): direct/group/failure/offline/cursor-migration paths.
.PHONY: test-daemon-localnet
test-daemon-localnet:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example chain_facts
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_daemon_localnet.py

.PHONY: test-testnet
test-testnet:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'
	$(RUN) node scripts/test_testnet_cache.cjs
