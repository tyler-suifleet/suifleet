"""Pinned JSON 1.0 device-metrics wire subset emitted by SF-062.
Validator only, never a metrics collector or transport substitute.
"""
import ipaddress
import copy


def validate_report(report):
    def obj(value, required, optional=()):
        assert isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional)
    def integer(value, minimum=0, maximum=2**64-1):
        assert type(value) is int and minimum <= value <= maximum
    obj(report, ("header", "metrics"))
    obj(report["header"], ("report_id", "version"))
    assert report["header"]["version"] == "1.0"
    integer(report["header"]["report_id"], 1)
    metrics = report["metrics"]
    obj(metrics, ("listening_tcp_ports", "listening_udp_ports", "tcp_connections"), ("network_stats",))
    for key in ("listening_tcp_ports", "listening_udp_ports"):
        value = metrics[key]
        obj(value, ("ports", "total"))
        assert isinstance(value["ports"], list)
        integer(value["total"])
        assert value["total"] == len(value["ports"])
        for port in value["ports"]:
            obj(port, ("port",))
            integer(port["port"], 1, 65535)
    obj(metrics["tcp_connections"], ("established_connections",))
    connections = metrics["tcp_connections"]["established_connections"]
    obj(connections, ("connections", "total"))
    assert isinstance(connections["connections"], list)
    integer(connections["total"])
    assert connections["total"] == len(connections["connections"])
    for connection in connections["connections"]:
        obj(connection, ("remote_addr", "local_port"))
        integer(connection["local_port"], 0, 65535)
        assert isinstance(connection["remote_addr"], str)
        host, port = connection["remote_addr"].rsplit(":", 1)
        ipaddress.ip_address(host.strip("[]"))
        integer(int(port), 0, 65535)
    if "network_stats" in metrics:
        obj(metrics["network_stats"], ("bytes_in", "bytes_out", "packets_in", "packets_out"))
        for value in metrics["network_stats"].values():
            integer(value)


def check_fixture(fixture):
    assert fixture["reference_version"] == "4.0.0" and fixture["schema_version"] == "1.0"
    assert fixture["topic"] == "$aws/things/thing1/defender/metrics/json"
    assert fixture["filters"] == [fixture["topic"] + "/" + result for result in ("accepted", "rejected")]
    validate_report(fixture["report"])
    for case in fixture["invalid_reports"]:
        report = copy.deepcopy(fixture["report"])
        target = report
        for key in case["path"][:-1]:
            target = target[key]
        target[case["path"][-1]] = case["value"]
        try:
            validate_report(report)
        except (AssertionError, ValueError):
            pass
        else:
            raise AssertionError(f"accepted invalid Defender schema case {case}")
    return 4 + len(fixture["responses"]) + len(fixture["invalid_reports"])
