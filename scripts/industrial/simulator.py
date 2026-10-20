"""Run the deterministic Modbus and OPC UA simulators locally (SF-070).

    python3 -m industrial.simulator --modbus-port 5020 --opcua-port 4840 --tick-ms 1000

Values change only on each tick: Modbus input register 0 and OPC UA
ns=2;s=Counter count ticks, coil 0 toggles, and fixed values sit at Modbus
holding registers 0-1 (float32 21.5) and ns=2;s=Temperature (21.5). Both listen
on loopback by default with policy None and anonymous access: a test fixture,
not a device. Prints the endpoints as JSON, then runs until interrupted.
"""
import argparse
import json
import time

from . import modbus, opcua_codec as ua, opcua_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--modbus-port", type=int, default=5020)
    parser.add_argument("--opcua-port", type=int, default=4840)
    parser.add_argument("--unit-id", type=int, default=1)
    parser.add_argument("--tick-ms", type=int, default=1000)
    args = parser.parse_args()
    model = modbus.DataModel(unit=args.unit_id)
    model.set_value("holding_register", 0, "float32", 21.5)
    mb = modbus.Simulator(model, host=args.host, port=args.modbus_port)
    opc = opcua_server.Simulator(host=args.host, port=args.opcua_port)
    opc.add_variable("ns=2;s=Temperature", ua.DOUBLE, 21.5)
    opc.add_variable("ns=2;s=Counter", ua.INT32, 0)
    print(json.dumps({"modbus": f"{args.host}:{mb.port}", "opcua": opc.url}), flush=True)
    try:
        while True:
            time.sleep(args.tick_ms / 1000)
            model.tick()
            opc.tick()
    except KeyboardInterrupt:
        pass
    finally:
        mb.close()
        opc.close()


if __name__ == "__main__":
    main()
