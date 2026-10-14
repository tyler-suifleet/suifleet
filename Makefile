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

publish-contracts:
	$(RUN) bash scripts/redeploy.sh

dapp-install:
	$(RUN) bash -c "cd dapp && pnpm install --frozen-lockfile"

dapp-dev:
	$(RUN) bash -c "fuser -k 3000/tcp 2>/dev/null; sleep 1; cd dapp && pnpm dev --hostname 0.0.0.0"

dapp-build:
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
	$(MAKE) test-manifest
	$(RUN) bash -c "cd dapp && pnpm typecheck"

.PHONY: check check-interfaces test-interfaces toolchain-versions ci tracking-plan test-tracking

# Client Move calls/types must exist in the compiled contracts with matching arity.
check-interfaces:
	$(RUN) python3 scripts/check_contract_interfaces.py

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
CI_STEPS := test-contracts test-interfaces check-interfaces edge-test test-ota test-component dapp-typecheck dapp-build
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
	$(MAKE) test-contracts
	$(MAKE) test-interfaces
	$(MAKE) check-interfaces
	$(MAKE) edge-test
	$(MAKE) test-ota
	$(MAKE) test-component
	$(MAKE) dapp-typecheck
	$(MAKE) dapp-build
	git diff --check

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

test-manifest-live:
	$(RUN) python3 scripts/test_manifest_upgrades.py

# Real daemon over gRPC against a disposable localnet (start it first, as for
# test-manifest-live): direct/group/failure/offline/cursor-migration paths.
.PHONY: test-daemon-localnet
test-daemon-localnet:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_daemon_localnet.py
