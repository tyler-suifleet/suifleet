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
	$(RUN) bash -c "cd suifleet-edge && cargo test --locked && cargo test --locked --features pkcs11"

.PHONY: test-ota test-component dapp-typecheck

# The daemon speaks gRPC; the simulator is served through the test-only shim
# example (suifleet-edge/examples/grpc_sim_shim.rs).
test-ota:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
	$(RUN) python3 scripts/test_ota.py
	$(RUN) python3 scripts/test_ota.py --platform-installer
	$(RUN) python3 scripts/test_catalog_ota.py
	$(RUN) python3 scripts/test_dependency_ota.py
	$(RUN) python3 scripts/test_ota.py --firmware-helper
	$(RUN) python3 scripts/test_firmware_restart.py
	$(RUN) python3 scripts/test_supervisor.py
	$(RUN) python3 scripts/test_configuration.py
	$(RUN) python3 scripts/test_greengrass_runtime.py
	$(RUN) python3 scripts/test_daemon_update.py
	$(RUN) python3 scripts/test_desired_state.py
	$(RUN) python3 scripts/test_handover_ota.py
	$(RUN) python3 scripts/test_deployment_control.py
	$(RUN) python3 scripts/test_telemetry.py
	$(RUN) python3 scripts/test_logs.py
	$(RUN) python3 scripts/test_cloudwatch_logs.py
	$(RUN) python3 scripts/test_rollout.py

# Fleet service configuration, root only in a disposable network-none container
# for real SO_PEERCRED principals (no host credentials or public network).
.PHONY: test-services
test-services:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
ifdef INSIDE_DEV_CONTAINER
	python3 scripts/test_services.py
else
	docker run --rm --network none --user 0:0 -e PYTHONDONTWRITEBYTECODE=1 -v "$(CURDIR):/workspace" -w /workspace "$${SUIFLEET_DEV_IMAGE:-$$(docker compose images -q dev)}" python3 scripts/test_services.py
endif

# Supervised components under the shipped systemd units on real systemd and
# cgroup v2 (SF-014): a disposable privileged container with systemd as PID 1.
# Runs on the host (needs Docker), not inside the dev container.
.PHONY: test-supervisor-systemd test-daemon-update-systemd test-key-isolation-systemd
# Daemon self-update under the shipped unit on real systemd (SF-026); host, needs Docker.
test-daemon-update-systemd:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
	PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_daemon_update_systemd.py --evidence "$${SUIFLEET_EVIDENCE:-daemon-update-systemd-evidence.json}"

# Device-key isolation under the shipped units on real systemd (SF-053); host, needs Docker.
test-key-isolation-systemd:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
	PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_key_isolation_systemd.py --evidence "$${SUIFLEET_EVIDENCE:-key-isolation-systemd-evidence.json}"

.PHONY: test-supervisor-systemd
test-supervisor-systemd:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
	PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_supervisor_systemd.py --evidence "$${SUIFLEET_EVIDENCE:-supervisor-systemd-evidence.json}"

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
	$(RUN) node scripts/test_component_catalog.cjs
	$(RUN) node scripts/test_deployment_control.cjs
	$(RUN) node scripts/test_rollout.cjs

generate-interfaces:
	$(RUN) python3 scripts/generate_interfaces.py

# Disposable localnet only; run inside a container with no public-network access.
test-protocol-live:
	$(RUN) python3 scripts/test_protocol_localnet.py

.PHONY: test-handover-upgrade-localnet
test-handover-upgrade-localnet:
	$(RUN) python3 scripts/test_protocol_localnet.py --handover-upgrade

.PHONY: test-catalog-live
test-catalog-live:
	$(RUN) python3 scripts/test_catalog_localnet.py

# Independent media decode, protocol-emulator and process-recovery suite.
.PHONY: test-video
test-video:
	bash scripts/test_kvs_video.sh

# SF-028 Greengrass compatibility matrix and reference-model fixtures, and the
# SF-037 translator executing the recipe/managed-dependency fixtures
# (docs/GREENGRASS_COMPATIBILITY.md). Offline; needs the dApp dependencies.
.PHONY: test-compat
test-compat:
	$(RUN) python3 scripts/greengrass_compat.py
	$(RUN) python3 scripts/greengrass_roadmap.py
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_greengrass_roadmap.py'
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_greengrass_compat.py'
	$(RUN) node scripts/test_greengrass_compat.cjs

# Exact wheel hashes are reviewed in interfaces/compat/ml/runtime-lock.json.
# Only download needs network; inference tests use the verified local cache.
.PHONY: test-ml
test-ml:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
	$(RUN) python3 scripts/ml_component.py download --platform linux-x86_64-cp310 --wheels .ci-cache/ml-wheels
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_ml_components.py --wheels .ci-cache/ml-wheels -v
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_ml_ota.py --wheels .ci-cache/ml-wheels

# Industrial collector (SF-070). Only industrial-deps uses the network: it
# unpacks the hash-pinned cryptography wheels needed by OPC UA Basic256Sha256.
INDUSTRIAL_RUNTIME ?= .ci-cache/industrial-runtime
.PHONY: industrial-deps test-industrial test-industrial-daemon
industrial-deps:
	$(RUN) python3 scripts/industrial/deps.py --dest $(INDUSTRIAL_RUNTIME)

test-industrial: industrial-deps
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$(INDUSTRIAL_RUNTIME)/site python3 scripts/test_industrial.py -v

# The built collector as a separate component UID against the real daemon:
# root only inside a disposable, network-isolated container.
test-industrial-daemon: industrial-deps
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
ifdef INSIDE_DEV_CONTAINER
	python3 scripts/test_industrial_daemon.py $(INDUSTRIAL_RUNTIME)/site $(INDUSTRIAL_EVIDENCE)
else
	docker run --rm --network none --user 0:0 -e PYTHONDONTWRITEBYTECODE=1 -v "$(CURDIR):/workspace" -w /workspace "$${SUIFLEET_DEV_IMAGE:-$$(docker compose images -q dev)}" python3 scripts/test_industrial_daemon.py $(INDUSTRIAL_RUNTIME)/site $(INDUSTRIAL_EVIDENCE)
endif

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
CI_STEPS := indexer-test test-testnet test-publish test-contracts test-interfaces check-interfaces test-compat test-ml test-industrial edge-test test-ota test-component test-edge-acceptance dapp-typecheck dapp-build
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
	$(MAKE) indexer-test
	$(MAKE) test-manifest
	$(MAKE) test-publish
	$(MAKE) test-contracts
	$(MAKE) test-interfaces
	$(MAKE) check-interfaces
	$(MAKE) test-compat
	$(MAKE) test-ml
	$(MAKE) test-industrial
	$(MAKE) edge-test
	$(MAKE) test-ota
	$(MAKE) test-component
	$(MAKE) test-edge-acceptance
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
# aarch64-unknown-linux-gnu or i686-unknown-linux-gnu (32-bit x86, docs/LINUX_X86.md).
RELEASE_TARGET ?= aarch64-unknown-linux-gnu

release-image:
	docker build -f docker/release.Dockerfile --build-arg USER_UID=$(UID) --build-arg USER_GID=$(GID) -t $(RELEASE_IMAGE) docker/

# Builds the committed HEAD twice and fails unless the bundles are identical.
release-artifacts: release-image
	python3 scripts/release.py build --out $(RELEASE_OUT) --image $(RELEASE_IMAGE) --target $(RELEASE_TARGET) --reproduce

# 32-bit Linux x86 (SF-076, docs/LINUX_X86.md), on the host with Docker: an
# i686 cross build in the release image, then the real-daemon simulator suites
# in a 32-bit i386 userland. LINUX_X86_BUNDLE=dir also checks a 32-bit release
# bundle (make release-artifacts RELEASE_TARGET=i686-unknown-linux-gnu).
.PHONY: linux-x86-image test-linux-x86
LINUX_X86_IMAGE ?= suifleet-linux-x86:local
linux-x86-image:
	docker build -f docker/linux-x86.Dockerfile --build-arg USER_UID=$(UID) --build-arg USER_GID=$(GID) -t $(LINUX_X86_IMAGE) docker/

test-linux-x86: release-image linux-x86-image
	PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_linux_x86.py --release-image $(RELEASE_IMAGE) --runner-image $(LINUX_X86_IMAGE) \
		$(if $(LINUX_X86_BUNDLE),--bundle $(LINUX_X86_BUNDLE)) $(if $(LINUX_X86_EVIDENCE),--evidence $(LINUX_X86_EVIDENCE))

# x86 catalog manifests across a real base-to-current package upgrade, on a
# disposable network-none localnet (host, needs Docker and a warm Move cache).
.PHONY: test-x86-catalog-localnet
X86_CATALOG_BASE ?= origin/main
test-x86-catalog-localnet:
	rm -rf .x86-catalog-base && mkdir .x86-catalog-base && git archive $(X86_CATALOG_BASE) contracts | tar -x -C .x86-catalog-base
	docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$(CURDIR):/workspace" -w /workspace \
		-v "$${SUIFLEET_MOVE_CACHE:-$(notdir $(CURDIR))_move-cache}:/home/dev/.move" "$${SUIFLEET_DEV_IMAGE:-$$(docker compose images -q dev)}" \
		python3 scripts/test_x86_catalog_localnet.py --base-contracts .x86-catalog-base/contracts; \
		status=$$?; rm -rf .x86-catalog-base; exit $$status

# make release-verify BUNDLE=dist TRUST=release/trust.toml
release-verify:
	$(RUN) bash -c "cd suifleet-edge && cargo run --locked --quiet --bin suifleet -- --verify-release ../$(BUNDLE) ../$(TRUST)"

tracking-plan:
	python3 scripts/bootstrap_tracking.py

test-tracking:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_tracking.py'

# Offline: disk audit and cleanup safety (docs/DISK_CLEANUP.md); temp dirs only.
.PHONY: test-disk-cleanup
test-disk-cleanup:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_disk_cleanup.py'

.PHONY: test-manifest test-manifest-live
test-manifest:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_deployment_manifest.py'
	$(RUN) node scripts/test_deployment_manifest.cjs
	$(RUN) node scripts/test_wallet_manifest.cjs
	$(RUN) node scripts/test_deployment_receipts.cjs
	$(RUN) node scripts/test_frontend_queries.cjs
	$(RUN) node scripts/test_target_groups.cjs
	$(RUN) node scripts/test_desired_state.cjs
	$(RUN) node scripts/test_telemetry.cjs
	$(RUN) node scripts/test_alerts.cjs

test-manifest-live:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example chain_facts
	$(RUN) python3 scripts/test_manifest_upgrades.py

# SF-013 desired state on a disposable localnet (start it first): main's
# contracts upgraded in place, then reconciliation judged by on-chain receipts.
.PHONY: test-desired-state-localnet
test-desired-state-localnet:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_desired_state_localnet.py

# SF-024 fleet indexer, API and CLI (suifleet-indexer/, docs/INDEXER.md).
.PHONY: indexer-build indexer-test test-indexer-localnet
indexer-build:
	$(RUN) cargo build --locked --manifest-path suifleet-indexer/Cargo.toml

indexer-test:
	$(RUN) cargo test --locked --manifest-path suifleet-indexer/Cargo.toml

# Against a disposable localnet (start it first): API vs chain, restart,
# auth, signed CLI bulk operation, chain-binding refusal and load.
test-indexer-localnet: indexer-build
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example chain_facts
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_indexer_localnet.py

# SF-006 publisher against a disposable localnet (start it first): refusals, dry
# run, fresh publish, upgrade, incompatible upgrade; both clients verify.
.PHONY: test-publish-localnet
test-publish-localnet:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example chain_facts
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_publish_localnet.py

# SF-021 on a disposable localnet (start it first): claim enrollment, delegated
# deployer roles and a SoftHSM2 PKCS#11 signer (needs softhsm2 + opensc).
.PHONY: test-enrollment-localnet
test-enrollment-localnet:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example chain_facts --features pkcs11
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_enrollment_localnet.py

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

# Old-package/legacy-object attacks across a real upgrade. The package pair is
# cached by contract content (~0.05 SUI reused; FRESH=1 republishes, ~0.22 SUI).
live-gate-upgrade-security:
	@test -n "$(SUIFLEET_WALLET)" || { echo "set SUIFLEET_WALLET to a funded wallet's client.yaml"; exit 1; }
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example chain_facts
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 scripts/live_upgrade_security.py --wallet "$(SUIFLEET_WALLET)" \
		--network "$${SUIFLEET_NETWORK:-testnet}" $(if $(SUIFLEET_STATE),--state "$(SUIFLEET_STATE)") $(if $(FRESH),--fresh) \
		--evidence "$${SUIFLEET_EVIDENCE:-live-security-evidence.json}"

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
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_live_common.py'
	$(RUN) node scripts/test_testnet_cache.cjs

.PHONY: test-deployment-control-localnet
test-deployment-control-localnet:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
	$(RUN) python3 scripts/test_deployment_control_localnet.py

# Real daemon, component UIDs and Eclipse Mosquitto; host Docker, host network.
.PHONY: test-messaging
test-messaging:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
	python3 scripts/test_messaging.py --image "$${SUIFLEET_DEV_IMAGE:-$$(docker compose images -q dev)}"

# Start a disposable indexed loopback node/faucet first; network-none Docker only.
.PHONY: test-rollout-localnet
test-rollout-localnet:
	$(RUN) python3 scripts/test_rollout_localnet.py

# SF-065 wallet-to-edge acceptance harness for SF-031 (docs/EDGE_ACCEPTANCE.md).
# Offline: profiles, matrix, locks, SSH pinning, fake-fleet runs with the real updater.
.PHONY: test-edge-acceptance test-edge-acceptance-localnet edge-acceptance
test-edge-acceptance:
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_edge_acceptance.py'

# The harness end to end on a disposable localnet (start it first).
test-edge-acceptance-localnet:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example chain_facts
	$(RUN) env PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_edge_acceptance_localnet.py

# An operator run on the host (needs sui, ssh and ticket on PATH):
#   make edge-acceptance PROFILE=p.json EVIDENCE=e.json [HARDWARE=1] LOCK_ARGS="--agent A --model M --session S"
edge-acceptance:
	@test -n "$(PROFILE)" && test -n "$(EVIDENCE)" || { echo "set PROFILE and EVIDENCE (docs/EDGE_ACCEPTANCE.md)"; exit 1; }
	PYTHONDONTWRITEBYTECODE=1 python3 scripts/edge_acceptance.py run --profile "$(PROFILE)" --evidence "$(EVIDENCE)" \
		$(if $(HARDWARE),--hardware) $(LOCK_ARGS)

# LAN client devices (SF-035): real daemon, Mosquitto and two TLS clients on an
# internal Docker network without an Internet route; host Docker.
.PHONY: test-client-devices
test-client-devices:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
	python3 scripts/test_client_devices.py --image "$${SUIFLEET_DEV_IMAGE:-$$(docker compose images -q dev)}" $(CLIENT_DEVICES_EVIDENCE)

# Streams (SF-034): real daemon, component UIDs and a verifying export
# receiver; root only inside a disposable, network-isolated container.
.PHONY: test-streams
test-streams:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
ifdef INSIDE_DEV_CONTAINER
	python3 scripts/test_streams.py $(STREAMS_EVIDENCE)
else
	docker run --rm --network none --user 0:0 -e PYTHONDONTWRITEBYTECODE=1 -v "$(CURDIR):/workspace" -w /workspace "$${SUIFLEET_DEV_IMAGE:-$$(docker compose images -q dev)}" python3 scripts/test_streams.py $(STREAMS_EVIDENCE)
endif

# Device shadows (SF-033): real daemon, component UIDs and a verifying
# reference cloud shadow service; root only inside a disposable,
# network-isolated container.
.PHONY: test-shadows
test-shadows:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
ifdef INSIDE_DEV_CONTAINER
	python3 scripts/test_shadows.py $(SHADOWS_EVIDENCE)
else
	docker run --rm --network none --user 0:0 -e PYTHONDONTWRITEBYTECODE=1 -v "$(CURDIR):/workspace" -w /workspace "$${SUIFLEET_DEV_IMAGE:-$$(docker compose images -q dev)}" python3 scripts/test_shadows.py $(SHADOWS_EVIDENCE)
endif

# Root only inside a disposable, network-isolated container: real SO_PEERCRED UIDs.
.PHONY: test-ipc
test-ipc:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
ifdef INSIDE_DEV_CONTAINER
	python3 scripts/test_ipc.py
else
	docker run --rm --network none --user 0:0 -e PYTHONDONTWRITEBYTECODE=1 -v "$(CURDIR):/workspace" -w /workspace "$${SUIFLEET_DEV_IMAGE:-$$(docker compose images -q dev)}" python3 scripts/test_ipc.py
endif

# CloudWatch Logs (SF-060) with the daemon and the adapter under separate UIDs;
# root only inside a disposable, network-isolated container.
.PHONY: test-cloudwatch-logs-uids
test-cloudwatch-logs-uids:
	$(RUN) cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
ifdef INSIDE_DEV_CONTAINER
	python3 scripts/test_cloudwatch_logs.py --distinct-uids
else
	docker run --rm --network none --user 0:0 -e PYTHONDONTWRITEBYTECODE=1 -v "$(CURDIR):/workspace" -w /workspace "$${SUIFLEET_DEV_IMAGE:-$$(docker compose images -q dev)}" python3 scripts/test_cloudwatch_logs.py --distinct-uids
endif

# SF-057: online public SDK preparation, then network-none actual adapter/mTLS tests.
.PHONY: test-aws-credentials
test-aws-credentials:
	bash scripts/test_aws_credentials.sh

# SF-059: actual metrics transport, persistence and peer authorization, offline.
.PHONY: test-cloudwatch-metrics
test-cloudwatch-metrics:
	bash scripts/test_cloudwatch_metrics.sh

# SF-058: actual durable daemon IPC + unmodified SDK against local AWS API emulators.
.PHONY: test-stream-export
test-stream-export:
	bash scripts/test_stream_export.sh

# SDK wheels are prepared by test-aws-credentials (or the command in FIREHOSE.md).
# All service/IPC traffic stays in a disposable offline container.
.PHONY: test-firehose
test-firehose:
	docker run --rm --network none --user 0:0 -e PYTHONDONTWRITEBYTECODE=1 \
	  -e PYTHONPATH=/workspace/.ci-cache/aws-python \
	  -v "$(CURDIR):/workspace" -w /workspace "$${SUIFLEET_DEV_IMAGE:?set pinned dev image}" \
	  sh -ec 'python3 -m unittest discover -s scripts/tests -p test_firehose_export.py -v; python3 scripts/test_streams.py --firehose'
