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

.PHONY: check tracking-plan test-tracking

# Sequential recipe: cross-layer checks must not race over shared build artifacts.
check:
	$(MAKE) test-contracts
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
