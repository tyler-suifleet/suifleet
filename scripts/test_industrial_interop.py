#!/usr/bin/env python3
"""SF-070 cross-implementation interoperability evidence (optional, host-run).

The collector and simulators are clean-room code; passing only against each
other would not show that they speak the protocols. This script exercises both
directions against independent open-source implementations, used as test-only
references and never shipped or copied:

  * the collector against an asyncua 1.1.5 OPC UA server (policy None with
    anonymous access, and Basic256Sha256 SignAndEncrypt with a UserName
    identity) and against a pymodbus 3.8.6 Modbus TCP server;
  * an asyncua client against the SuiFleet OPC UA simulator (same security
    combinations, Read and a data-change subscription) and a pymodbus client
    against the SuiFleet Modbus simulator (function codes 1-4 and an exception).

Install the references into a scratch directory, then point --reference-site at it:

    python3 -m pip install --target /tmp/ref 'asyncua==1.1.5' 'pymodbus==3.8.6'
    python3 scripts/test_industrial_interop.py --reference-site /tmp/ref [--evidence FILE]

asyncua is LGPL-3.0 and pymodbus BSD-3-Clause; both run only in this test process.
"""
import argparse
import asyncio
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def certificates(directory):
    for name, uri in (("server", "urn:suifleet:interop-server"), ("client", "urn:suifleet:industrial-collector")):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out",
                        f"{name}.pem", "-days", "2", "-subj", f"/CN=sf070-{name}", "-addext",
                        f"subjectAltName=URI:{uri},DNS:localhost,IP:127.0.0.1", "-addext",
                        "keyUsage=digitalSignature,nonRepudiation,keyEncipherment,dataEncipherment,keyCertSign",
                        "-addext", "extendedKeyUsage=serverAuth,clientAuth"],
                       check=True, capture_output=True, cwd=directory)
        subprocess.run(["openssl", "x509", "-in", f"{name}.pem", "-outform", "DER", "-out", f"{name}.der"],
                       check=True, capture_output=True, cwd=directory)
        (Path(directory) / f"{name}.key").chmod(0o600)
        # asyncua selects the key format by file extension.
        (Path(directory) / f"{name}-key.pem").write_bytes((Path(directory) / f"{name}.key").read_bytes())


class Loop:
    """A background asyncio loop for the asyncio-based references."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coroutine, timeout=60):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)


def wait_until(condition, timeout=15.0, message="condition not reached"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-site", required=True)
    parser.add_argument("--evidence")
    args = parser.parse_args()
    sys.path.insert(0, args.reference_site)
    logging.basicConfig(level=logging.ERROR)
    from asyncua import Client as RefClient, Server as RefServer, ua as aua
    from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
    from asyncua.server.user_managers import UserManager
    from asyncua.server.users import User, UserRole
    import cryptography
    import pymodbus
    from pymodbus.client import ModbusTcpClient
    from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext
    from pymodbus.server import StartAsyncTcpServer

    from industrial import collector as col, modbus, opcua_codec as ua, opcua_secure as sec, opcua_server
    from test_industrial import MemorySink

    passed = []

    def ok(text):
        passed.append(text)
        print(f"PASS: {text}", flush=True)

    loop = Loop()
    with tempfile.TemporaryDirectory(prefix="sf070-interop-") as tmp:
        d = Path(tmp)
        certificates(d)
        password = d / "password"
        password.write_text("pw\n")
        password.chmod(0o600)

        # --- Collector -> asyncua server -----------------------------------------------
        class Users(UserManager):
            def get_user(self, iserver, username=None, password=None, certificate=None):
                if username is None and password is None and certificate is None:
                    return User(role=UserRole.User)               # anonymous
                if username == "op" and password == "pw":
                    return User(role=UserRole.User)
                return None

        reference = {}

        async def start_server():
            server = RefServer(user_manager=Users())
            await server.init()
            server.set_endpoint("opc.tcp://127.0.0.1:48471/")
            server.set_server_name("SF-070 interop reference")
            await server.set_application_uri("urn:suifleet:interop-server")
            server.set_security_policy([aua.SecurityPolicyType.NoSecurity,
                                        aua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt])
            await server.load_certificate(str(d / "server.der"))
            await server.load_private_key(str(d / "server-key.pem"))
            idx = await server.register_namespace("urn:suifleet:interop")
            plant = await server.nodes.objects.add_object(idx, "Plant")
            reference["temperature"] = await plant.add_variable(aua.NodeId("Temperature", idx), "Temperature", 21.5,
                                                                varianttype=aua.VariantType.Double)
            reference["counter"] = await plant.add_variable(aua.NodeId("Counter", idx), "Counter", 0,
                                                            varianttype=aua.VariantType.Int32)
            reference["idx"] = idx
            await server.start()
            reference["server"] = server

        loop.run(start_server())
        idx = reference["idx"]

        def opc_source(name, **extra):
            return {"name": name, "protocol": "opcua", "endpoint_url": "opc.tcp://127.0.0.1:48471/",
                    "publishing_interval_ms": 100, "keepalive_count": 5, "lifetime_count": 50,
                    "connect_timeout_ms": 3000, "request_timeout_ms": 3000, "reconnect_min_ms": 200,
                    "reconnect_max_ms": 1000,
                    "points": [{"name": "counter", "node_id": f"ns={idx};s=Counter", "type": "int32",
                                "sampling_interval_ms": 50},
                               {"name": "temperature", "node_id": f"ns={idx};s=Temperature", "mode": "poll",
                                "poll_interval_ms": 200, "type": "double"}], **extra}

        secure = dict(security_policy="Basic256Sha256", security_mode="SignAndEncrypt",
                      certificate_file=str(d / "client.der"), private_key_file=str(d / "client.key"),
                      server_certificate_file=str(d / "server.der"),
                      identity={"type": "username", "username": "op", "password_file": str(password)})
        wrong = dict(secure, identity={"type": "username", "username": "op", "password_file": str(d / "wrong")})
        (d / "wrong").write_text("nope\n")
        (d / "wrong").chmod(0o600)
        config = {"version": 1, "collector_id": "interop", "sources": [
            opc_source("ref-none"), opc_source("ref-secure", **secure), opc_source("ref-wrong-password", **wrong)],
            "output": {"socket": "/run/suifleet/ipc.sock", "component": "industrial", "stream": "industrial"}}
        sink = MemorySink()
        collector = col.Collector(col.load_secrets(col.validate(config)), sink=sink, log=lambda _: None)
        collector.start()
        try:
            for value in range(1, 6):
                loop.run(reference["counter"].write_value(value, aua.VariantType.Int32))
                time.sleep(0.15)
            for name in ("ref-none", "ref-secure"):
                wait_until(lambda: [r for r in sink.records("sample", source=name, point="counter") if r["value"] == 5],
                           message=f"{name}: subscription did not deliver 5")
                temps = sink.records("sample", source=name, point="temperature")
                assert temps and temps[-1]["value"] == 21.5 and temps[-1]["type"] == "double", temps[-1:]
                counters = [r["value"] for r in sink.records("sample", source=name, point="counter")]
                assert counters == sorted(counters) and all(r["quality"] == "GOOD" and r["source_timestamp_ns"]
                                                            for r in sink.records("sample", source=name)), counters
            rejected = wait_until(lambda: sink.records("status", source="ref-wrong-password", state="rejected"))
            assert sink.records("sample", source="ref-wrong-password", quality="GOOD") == []
            ok(f"collector -> asyncua 1.1.5 server: policy None/anonymous and Basic256Sha256 "
               f"SignAndEncrypt/UserName subscription and Read; wrong password rejected ({rejected[0]['reason']})")
        finally:
            collector.stop(1.0)
            loop.run(reference["server"].stop())

        # --- asyncua client -> SuiFleet OPC UA simulator ------------------------------
        sim = opcua_server.Simulator(endpoints=[("None", "None"), ("Basic256Sha256", "SignAndEncrypt")],
                                     certificate=(d / "server.der").read_bytes(),
                                     private_key=sec.load_private_key(str(d / "server.key")),
                                     trusted_certificates=[(d / "client.der").read_bytes()], users={"op": "pw"},
                                     allow_anonymous=True)
        sim.add_variable("ns=2;s=Temperature", ua.DOUBLE, 21.5)
        sim.add_variable("ns=2;s=Counter", ua.INT32, 0)
        sim.add_variable("i=2259", ua.INT32, 0)                 # Server_ServerStatus_State: Running

        async def reference_client(secure_mode):
            client = RefClient(sim.url, timeout=5)
            if secure_mode:
                await client.set_security(SecurityPolicyBasic256Sha256, str(d / "client.der"), str(d / "client-key.pem"),
                                          server_certificate=str(d / "server.der"),
                                          mode=aua.MessageSecurityMode.SignAndEncrypt)
                client.set_user("op")
                client.set_password("pw")
            client.application_uri = "urn:suifleet:industrial-collector"
            received = []

            class Handler:
                def datachange_notification(self, node, value, data):
                    received.append(value)
            async with client:
                temperature = await client.get_node("ns=2;s=Temperature").read_value()
                subscription = await client.create_subscription(50, Handler())
                await subscription.subscribe_data_change(client.get_node("ns=2;s=Counter"))
                for _ in range(4):
                    sim.tick()
                    await asyncio.sleep(0.15)
                for _ in range(40):
                    if received and received[-1] == sim.nodes[ua.NodeId(2, "Counter")].value:
                        break
                    await asyncio.sleep(0.05)
                await subscription.delete()
            return temperature, received

        for secure_mode in (False, True):
            temperature, received = loop.run(reference_client(secure_mode))
            final = sim.nodes[ua.NodeId(2, "Counter")].value
            assert temperature == 21.5 and received and received[-1] == final and received == sorted(received), \
                (temperature, received, final)
        assert sim.counters["sessions"] >= 2
        ok("asyncua 1.1.5 client -> SuiFleet OPC UA simulator: policy None/anonymous and Basic256Sha256 "
           "SignAndEncrypt/UserName Read, subscription data changes and DeleteSubscriptions")

        async def wrong_reference_password():
            client = RefClient(sim.url, timeout=5)
            await client.set_security(SecurityPolicyBasic256Sha256, str(d / "client.der"), str(d / "client-key.pem"),
                                      server_certificate=str(d / "server.der"), mode=aua.MessageSecurityMode.SignAndEncrypt)
            client.set_user("op")
            client.set_password("wrong")
            try:
                async with client:
                    return "connected"
            except Exception as error:                     # noqa: BLE001 - the reference raises its own types
                return type(error).__name__ + ": " + str(error)
        rejected_before = sim.counters["activations_rejected"]
        outcome = loop.run(wrong_reference_password())
        # asyncua's teardown (CloseSession on the unactivated session) can mask the ActivateSession fault.
        assert outcome != "connected" and sim.counters["activations_rejected"] == rejected_before + 1, outcome
        ok("SuiFleet simulator rejects a wrong asyncua password at ActivateSession")
        sim.close()

        # --- Collector -> pymodbus server -----------------------------------------------
        hr = [0] * 20
        hr[0:2] = modbus.encode_registers("float32", 21.5)
        hr[2:4] = modbus.encode_registers("int32", -7, "little")
        coils = [False] * 16
        coils[3] = True
        # pymodbus data blocks map protocol address a to block address a + 1 (its default zero_mode=False).
        store = ModbusSlaveContext(hr=ModbusSequentialDataBlock(1, hr), ir=ModbusSequentialDataBlock(1, [9, 8, 7, 6]),
                                   co=ModbusSequentialDataBlock(1, coils), di=ModbusSequentialDataBlock(1, [True] * 16))
        context = ModbusServerContext(slaves={17: store}, single=False)
        loop.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(
            StartAsyncTcpServer(context=context, address=("127.0.0.1", 48502))))
        time.sleep(1.0)
        sink = MemorySink()
        config = {"version": 1, "collector_id": "interop", "output": {"socket": "/run/suifleet/ipc.sock",
                  "component": "industrial", "stream": "industrial"}, "sources": [{
                  "name": "pymodbus", "protocol": "modbus", "transport": "tcp", "host": "127.0.0.1", "port": 48502,
                  "unit_id": 17, "poll_interval_ms": 100, "points": [
                      {"name": "temperature", "table": "holding_register", "address": 0, "type": "float32"},
                      {"name": "energy", "table": "holding_register", "address": 2, "type": "int32", "word_order": "little"},
                      {"name": "input", "table": "input_register", "address": 1, "type": "uint16"},
                      {"name": "running", "table": "coil", "address": 3, "type": "bool"},
                      {"name": "door", "table": "discrete_input", "address": 7, "type": "bool"},
                      {"name": "missing", "table": "holding_register", "address": 5000, "type": "uint16"}]}]}
        collector = col.Collector(col.load_secrets(col.validate(config)), sink=sink, log=lambda _: None)
        collector.start()
        try:
            expected = {"temperature": 21.5, "energy": -7, "input": 8, "running": True, "door": True}
            wait_until(lambda: all(sink.records("sample", point=p) for p in [*expected, "missing"]))
            got = {p: sink.records("sample", point=p)[-1]["value"] for p in expected}
            missing = sink.records("sample", point="missing")[-1]
            assert got == expected and missing["quality"] == "BAD" and missing["status"].startswith("exception:2"), (got, missing)
            ok(f"collector -> pymodbus {pymodbus.__version__} TCP server: function codes 1-4, word order, "
               f"and exception 2 for an unmapped address ({missing['status']})")
        finally:
            collector.stop(1.0)

        # --- pymodbus client -> SuiFleet Modbus simulator --------------------------------
        model = modbus.DataModel(unit=17)
        model.set_value("holding_register", 0, "float32", 21.5)
        model.set_value("input_register", 4, "uint16", 4242)
        model.set_bit("coil", 2, True)
        model.set_bit("discrete_input", 1, True)
        model.exceptions[(3, 9)] = 2
        mb = modbus.Simulator(model)
        client = ModbusTcpClient("127.0.0.1", port=mb.port, timeout=3)
        assert client.connect()
        try:
            regs = client.read_holding_registers(0, count=2, slave=17)
            assert not regs.isError() and modbus.decode_registers("float32", regs.registers) == 21.5, regs
            inputs = client.read_input_registers(4, count=1, slave=17)
            assert inputs.registers == [4242], inputs
            assert client.read_coils(0, count=3, slave=17).bits[:3] == [False, False, True]
            assert client.read_discrete_inputs(0, count=2, slave=17).bits[:2] == [False, True]
            error = client.read_holding_registers(9, count=1, slave=17)
            assert error.isError() and error.exception_code == 2, error
        finally:
            client.close()
            mb.close()
        ok(f"pymodbus {pymodbus.__version__} client -> SuiFleet Modbus simulator: function codes 1-4 and exception 2")

        if args.evidence:
            head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True, capture_output=True).stdout.strip()
            Path(args.evidence).write_text(json.dumps({
                "ticket": "SF-070", "head": head or None, "checks": passed,
                "references": {"asyncua": "1.1.5", "pymodbus": pymodbus.__version__, "cryptography": cryptography.__version__,
                               "python": sys.version.split()[0]}}, indent=2) + "\n")


if __name__ == "__main__":
    main()
