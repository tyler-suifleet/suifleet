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

.PHONY: test-ota dapp-typecheck

test-ota:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml
	$(RUN) python3 scripts/test_ota.py

dapp-typecheck:
	$(RUN) bash -c "cd dapp && pnpm typecheck"

.PHONY: check check-interfaces toolchain-versions ci tracking-plan test-tracking

# Client Move calls/types must exist in the compiled contracts with matching arity.
check-interfaces:
	$(RUN) python3 scripts/check_contract_interfaces.py

toolchain-versions:
	@$(RUN) bash -c 'echo "sui: $$(sui --version)"; echo "rustc: $$(rustc --version)"; \
		echo "cargo: $$(cargo --version)"; echo "node: $$(node --version)"; echo "pnpm: $$(pnpm --version)"; \
		echo "python3: $$(python3 --version 2>&1)"'

# CI entry point (run inside the dev image): each step's output is kept under
# ci-logs/ for failure diagnostics. No step publishes, spends gas or uses secrets.
ci: SHELL := /bin/bash
CI_STEPS := test-contracts check-interfaces edge-test test-ota dapp-typecheck dapp-build
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
	$(MAKE) test-contracts
	$(MAKE) check-interfaces
	$(MAKE) edge-test
	$(MAKE) test-ota
	$(MAKE) dapp-typecheck
	$(MAKE) dapp-build
	git diff --check

tracking-plan:
	python3 scripts/bootstrap_tracking.py

test-tracking:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_tracking.py'

.PHONY: test-coordinator

test-coordinator:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_ticket_coordinator.py'
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tools/atomic-ticketing/tests -p 'test_*.py'

.PHONY: install-ticket-coordinator

install-ticket-coordinator:
	python3 tools/atomic-ticketing/install.py
