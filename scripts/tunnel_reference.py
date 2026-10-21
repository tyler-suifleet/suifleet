"""Independent notification/configuration reference vectors for SF-063.
No process/network work; runtime evidence is owned by test_tunneling.py.
"""
import ipaddress
import json
import re
from pathlib import PurePosixPath


def configuration(value):
    try:
        valid = value["enabled"] is True and re.fullmatch(r"[a-z0-9-]{1,32}", value["region"])
        valid = valid and all(PurePosixPath(value[p]).is_absolute() for p in ("proxy", "working_directory", "proxy_settings"))
        valid = valid and re.fullmatch(r"[A-Fa-f0-9]{64}", value["proxy_sha256"])
        valid = valid and 1 <= value["max_concurrent_tunnels"] <= 16 and 1 <= value["tunnel_timeout_seconds"] <= 43200
        targets = value["destinations"]
        valid = valid and 1 <= len(targets) <= 16
        for service, address in targets.items():
            host, port = address.rsplit(":", 1)
            valid = valid and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", service) and ipaddress.IPv4Address(host).is_loopback and 1 <= int(port) <= 65535
        return bool(valid)
    except (ValueError, KeyError, TypeError):
        return False


def notification(case, settings):
    try:
        raw = case.get("raw", json.dumps(case.get("payload")))
        if len(raw.encode()) > 16384 or case["topic"] != "$aws/things/thing1/tunnels/notify" or case["retain"]:
            return False
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate")
                result[key] = value
            return result
        body = json.loads(raw, object_pairs_hook=unique)
        if set(body) != {"clientAccessToken", "clientMode", "region", "services"}:
            return False
        services, token = body["services"], body["clientAccessToken"]
        return (body["clientMode"] == "destination" and body["region"] == settings["region"]
                and isinstance(services, list) and 1 <= len(services) <= 16
                and all(isinstance(s, str) and s in settings["destinations"] for s in services)
                and len(set(services)) == len(services)
                and isinstance(token, str) and 1 <= len(token) <= 4096
                and all(33 <= ord(c) <= 126 for c in token))
    except (ValueError, KeyError, TypeError):
        return False


def check_fixture(cases):
    count = 0
    for case in cases["notifications"]:
        assert notification(case, cases["settings"]) == case["accepted"], case["id"]
        count += 1
    for case in cases["configuration"]:
        assert configuration({**cases["settings"], **case["patch"]}) == case["accepted"], case["id"]
        count += 1
    return count
