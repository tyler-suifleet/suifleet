#!/usr/bin/env python3
"""Fleet budget gas top-ups (SF-083) with the real daemon against the local
chain simulator of scripts/test_ota.py, without keys or gas.

The simulator charges the device for every transaction and keeps the budget
and the device's account as fleet_budget::draw_gas would. Checked:

- a device below its low-water mark draws once from the budget, within the
  float cap, and then reports;
- with the period allowance used up and too little gas to report, the daemon
  draws nothing, records why in its journal (telemetry reports it) and keeps
  the receipt queued, across a restart;
- a new period restores the allowance: the daemon draws and the queued
  receipt goes out;
- a paused budget stops draws, and the reason changes accordingly;
- the simulator refused no draw: the daemon only submits draws the contract
  would accept.

This verifies daemon integration, not the contract (Move tests) or a public
network (scripts/live_fleet_budget.py).
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ota as ota
from deployment_manifest import BUDGET_TYPES, object_id

BUDGET = "0xb0"
CLOCK = "0x6"
DRAW_COST, REPORT_COST = 100, 400
LOW_WATER, DRAW = 1_000, 5_000
PERIOD_MS = 3_600_000


def now_ms():
    return int(time.time() * 1000)
for _name in BUDGET_TYPES:
    ota.TYPE_ORIGINS[_name] = ota.PACKAGE  # Introduced by a later upgrade.


class InsufficientGas(ConnectionError):
    """Gas selection fails before execution, as on a node: nothing is charged."""


class BudgetSimulator(ota.Simulator):
    def __init__(self):
        super().__init__()
        self.allow_reports = True
        self.fail_execution = False
        self.gas = 500
        self.budget = {"version": "1", "paused": False, "balance": {"value": "100000"}, "registry_id": ota.REGISTRY}
        self.account = {"owner_cap_id": ota.OWNER_CAP, "suspended": False, "enrolled_at_ms": "1", "total_drawn": "0",
            "limits": {"float_cap": "6000", "period_ms": str(PERIOD_MS), "period_allowance": "10000",
                       "max_draws_per_period": "3", "min_draw_interval_ms": "0"},
            "period_start_ms": str(now_ms()), "drawn_in_period": "0", "draws_in_period": "0", "last_draw_ms": "0"}
        self.draws = []
        self.refused = []

    def obj(self, id):
        if id == BUDGET:
            return {"data": {"objectId": id, "owner": {"Shared": {"initial_shared_version": "1"}},
                "content": {"type": f"{ota.TYPE_ORIGINS['fleet_budget::FleetBudget']}::fleet_budget::FleetBudget", "fields": self.budget}}}
        return super().obj(id)

    def refusal(self, amount):
        """What fleet_budget::draw_gas would abort with, if anything."""
        limits, account = self.account["limits"], self.account
        if now_ms() >= int(account["period_start_ms"]) + int(limits["period_ms"]):
            account.update(period_start_ms=str(now_ms()), drawn_in_period="0", draws_in_period="0")
        if self.budget["paused"]:
            return "EPaused"
        if account["suspended"]:
            return "ESuspended"
        if amount > int(limits["float_cap"]):
            return "EOverFloat"
        if int(account["draws_in_period"]) >= int(limits["max_draws_per_period"]):
            return "ERateLimited"
        if int(account["drawn_in_period"]) + amount > int(limits["period_allowance"]):
            return "EOverAllowance"
        if int(self.budget["balance"]["value"]) < amount:
            return "EInsufficientBalance"
        return None

    def rpc(self, method, params):
        if method == "shim_balance":
            assert params == [ota.ADDRESS, "0x2::sui::SUI"], params
            return str(self.gas)
        if method == "shim_fields":
            key = f"{ota.TYPE_ORIGINS['fleet_budget::DeviceKey']}::fleet_budget::DeviceKey"
            return super().rpc(method, params) + [[BUDGET, key, object_id(self.device)[2:]]]
        if method == "suix_getDynamicFieldObject" and params[0] == BUDGET:
            assert params[1]["type"].endswith("::fleet_budget::DeviceKey")
            return {"data": {"content": {"fields": {"value": self.account}}}}
        if method == "unsafe_moveCall" and params[2] == "fleet_budget":
            assert params[0] == ota.ADDRESS and params[1] == ota.PACKAGE and params[3] == "draw_gas", params
            budget, registry, cap, amount, clock = params[5]
            assert (budget, registry, cap, clock) == (BUDGET, ota.REGISTRY, self.cap, CLOCK), params
            amount = int(amount)
            if self.gas < DRAW_COST:
                raise InsufficientGas("insufficient gas for the draw")
            refused = self.refusal(amount)
            if refused:
                # The daemon checks first; a refused draw must never be built.
                self.refused.append((refused, amount))
                raise AssertionError(f"draw_gas would abort with {refused}")
            self.built.append(("draw", amount))
            return {"txBytes": "AQIDBA=="}
        if method == "unsafe_moveCall":
            if self.gas < REPORT_COST:
                raise InsufficientGas("insufficient gas for the report")
            return super().rpc(method, params)
        if method == "sui_executeTransactionBlock":
            assert params[1] == [f"verified:{ota.ADDRESS}"], params[1]
            kind, value = self.built[-1]
            if kind == "draw":
                self.gas += value - DRAW_COST
                self.budget["balance"]["value"] = str(int(self.budget["balance"]["value"]) - value)
                for field, delta in (("drawn_in_period", value), ("draws_in_period", 1), ("total_drawn", value)):
                    self.account[field] = str(int(self.account[field]) + delta)
                self.account["last_draw_ms"] = str(now_ms())
                self.draws.append(value)
                return {"digest": f"draw-{len(self.draws)}", "effects": {"status": {"status": "success"}}}
            self.gas -= REPORT_COST
            self.reports.append(self.built[-1])
            return {"digest": "report-digest", "effects": {"status": {"status": "success"}}}
        return super().rpc(method, params)


def run(binary):
    sim = BudgetSimulator()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, body, status=200):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                response = {"jsonrpc": "2.0", "id": request["id"], "result": sim.rpc(request["method"], request["params"])}
            except Exception as error:
                if not isinstance(error, ConnectionError):
                    sim.errors.append(repr(error))
                response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000, "message": str(error)}}
            self.respond(json.dumps(response).encode())

        def do_GET(self):
            blob = self.path.removeprefix("/v1/blobs/")
            if blob == "artifact":
                self.respond(ota.ARTIFACT)
            elif blob in sim.recipes:
                self.respond(json.dumps(sim.recipes[blob]).encode())
            else:
                self.respond(b"not found", 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shim, rpc_url = ota.start_grpc(f"http://127.0.0.1:{server.server_port}")
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="suifleet-budget-") as temporary:
            root = Path(temporary)
            key = root / "device.key"
            key.write_text("07" * 32)
            key.chmod(0o600)
            applied = root / "applied.jsonl"
            updater = root / "apply.py"
            updater.write_text("import pathlib, sys\nwith open(" + repr(str(applied)) + ", 'a') as output:\n    output.write('applied\\n')\n")
            url = f"http://127.0.0.1:{server.server_port}"
            manifest = dict(schema_version=1, protocol_version=1, network="localnet", chain_identifier=sim.chain,
                rpc_url=rpc_url, call_package_id=ota.PACKAGE, upgrade_cap_id=ota.UPGRADE_CAP, package_version="3", type_origins=ota.TYPE_ORIGINS,
                shared_objects={"registry": {"id": ota.REGISTRY, "initial_shared_version": "1"}},
                supported_clients={"dapp": {"min": "0.0.1", "max_exclusive": "0.1.0"}, "edge": {"min": "0.1.0", "max_exclusive": "0.3.0"}},
                walrus={"publisher_url": url, "aggregator_url": url})
            (root / "deployment.json").write_text(json.dumps(manifest))
            config = root / "config.toml"
            config.write_text('[sui]\nmanifest_path = "deployment.json"\n'
                f'[device]\nkeypair_path = {json.dumps(str(key))}\ndevice_object_id = "{ota.DEVICE}"\ndevice_cap_id = "{ota.CAP}"\n'
                f'[walrus]\naggregator_url = "{url}"\n[apply]\ncommand = {json.dumps(f"{sys.executable} {updater}")}\ntimeout_seconds = 2\n'
                f'[runtime]\nstate_dir = {json.dumps(str(root / "state"))}\npoll_seconds = 1\n'
                f'[budget]\nbudget_id = "{BUDGET}"\nlow_water_mist = {LOW_WATER}\ndraw_mist = {DRAW}\n')

            def state():
                path = root / "state" / "state.json"
                return json.loads(path.read_text()) if path.exists() else {}

            def funding():
                return state().get("funding") or {}

            def start():
                return subprocess.Popen([str(binary)], env={**os.environ, "EDGE_CONFIG": str(config)}, stdout=log, stderr=log)

            def stop(process):
                process.send_signal(signal.SIGINT)
                process.wait(timeout=10)
                assert process.returncode == 0

            def reported(id):
                return state().get("deployments", {}).get(id, {}).get("reported")

            sim.add("0x200")  # Historical: never applied.
            try:
                with (root / "daemon.log").open("w") as log:
                    process = start()
                    # Below low water: one draw of the float cap (asked 5000, cap 6000 → 5000).
                    ota.wait_for(lambda: funding().get("draws") == 1, "daemon did not draw from the budget", process)
                    assert sim.draws == [DRAW] and sim.gas == 500 + DRAW - DRAW_COST, (sim.draws, sim.gas)
                    assert funding()["last_draw_digest"] == "draw-1" and funding().get("blocked") is None, funding()
                    ota.wait_for(lambda: "after" in state().get("cursor", {}), "first event scan did not complete", process)
                    sim.add("0x201")
                    ota.wait_for(lambda: reported("0x201"), "funded device did not report", process)
                    print("PASS: low balance drew once within the float cap, then the receipt was reported")

                    # Allowance used up, too little gas to report: queue and explain.
                    sim.account["drawn_in_period"] = sim.account["limits"]["period_allowance"]
                    sim.gas = REPORT_COST - 1
                    sim.add("0x202")
                    ota.wait_for(lambda: "0x202" in state().get("deployments", {}), "deployment not applied", process)
                    ota.wait_for(lambda: "used up" in (funding().get("blocked") or ""), "exhaustion not recorded", process)
                    time.sleep(2.5)
                    assert reported("0x202") is False and sim.draws == [DRAW], (state()["deployments"]["0x202"], sim.draws)
                    stop(process)
                    process = None
                    # Durable across a restart: still queued, still explained.
                    assert reported("0x202") is False and "used up" in funding()["blocked"]
                    process = start()
                    time.sleep(2.5)
                    assert reported("0x202") is False and len(sim.draws) == 1
                    print("PASS: exhausted allowance drew nothing, kept the receipt queued and recorded why, across a restart")

                    # A new period: draw again and flush the queue.
                    sim.account["period_start_ms"] = str(now_ms() - PERIOD_MS)
                    ota.wait_for(lambda: reported("0x202"), "queued receipt was not reported after recovery", process)
                    assert sim.draws == [DRAW, DRAW] and funding().get("blocked") is None and funding()["draws"] == 2, funding()
                    print("PASS: a new period restored the allowance; the daemon drew and the queued receipt went out")

                    # The owner pauses the budget.
                    sim.budget["paused"] = True
                    sim.gas = LOW_WATER - 1
                    ota.wait_for(lambda: "paused" in (funding().get("blocked") or ""), "pause not recorded", process)
                    assert len(sim.draws) == 2
                    print("PASS: a paused budget stopped draws and the reason was recorded")
                    stop(process)
                    process = None
            except BaseException:
                lines = (root / "daemon.log").read_text().splitlines()[-30:]
                print("daemon log tail:\n" + "\n".join(lines), file=sys.stderr)
                raise
            assert not sim.refused and not sim.errors, (sim.refused, sim.errors)
            print("PASS: the simulator refused no draw: the daemon submits only draws the contract accepts")
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        shim.kill()
        shim.wait()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path(__file__).resolve().parents[1] / "suifleet-edge/target/debug/suifleet")
    run(parser.parse_args().binary.resolve())
