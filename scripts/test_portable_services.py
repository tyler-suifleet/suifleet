"""Real manager lifecycle checks using test_key_isolation_systemd's OTA fixture."""

from pathlib import Path
import os
import signal
import subprocess


PROFILES = Path("/workspace/suifleet-edge/packaging/portable")
LAUNCHER = "/usr/libexec/suifleet-portable-signer"


def command(*args):
    subprocess.run(args, check=True, timeout=30)


def processes(name):
    pids = set()
    for comm in Path("/proc").glob("[0-9]*/comm"):
        try:
            if comm.read_text().strip() == name:
                pids.add(int(comm.parent.name))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return pids


def children(pid):
    return {int(child) for child in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()}


def exercise(profile, sock, key, state, journal, wait, sim, check):
    command("install", "-D", "-m", "0755", "/workspace/scripts/suifleet-portable-signer", LAUNCHER)
    manual = None
    runsv = None
    if profile in ("sysv", "openrc"):
        command("install", "-m", "0755", PROFILES / profile / "suifleet", "/etc/init.d/suifleet")
    if profile == "runit":
        service = Path("/etc/sv/suifleet")
        command("install", "-D", "-m", "0755", PROFILES / profile / "suifleet/run", service / "run")
        (service / "down").touch()
        runsv = subprocess.Popen(["runsv", service])
        wait(lambda: (service / "supervise/ok").exists(), "runsv did not initialize", 10)

    def control(action):
        nonlocal manual
        if profile == "openrc":
            command("rc-service", "suifleet", action)
        elif profile == "sysv":
            command("/etc/init.d/suifleet", action)
        elif profile == "runit":
            command("sv", "-w", "20", {"start": "up", "stop": "down", "restart": "restart"}[action], service)
        else:
            if action in ("stop", "restart") and manual is not None:
                if manual.poll() is None:
                    manual.terminate()
                manual.wait(timeout=20)
            if action in ("start", "restart"):
                manual = subprocess.Popen([LAUNCHER])

    def report(deployment):
        sim.add(deployment, recipe={"lifecycle": {"install": "/bin/true"}})
        wait(lambda: (deployment, 1) in sim.reports, f"no independently verified Applied receipt for {deployment}", 25)

    def running():
        launchers = processes("suifleet-pair")
        if len(launchers) != 1 or not sock.exists():
            return False
        pid, = launchers
        return pid if len(children(pid)) == 2 else False

    def pair():
        wait(running, "launcher and both children did not start", 25)
        pid = running()
        return pid, children(pid)

    # Exercise the real process supervisor, signer and an interrupted recipe.
    key_bytes = key.read_bytes()
    address = subprocess.check_output(["/usr/bin/suifleet", "--provision", key])
    assert address == subprocess.check_output(["/usr/bin/suifleet", "--derive-address", key])
    try:
        control("start")
        wait(lambda: journal().get("initialized"), "daemon did not initialize", 25)
        report("0x921")
        launcher, workers = pair()
        assert {Path(f"/proc/{pid}").stat().st_uid for pid in workers} == {key.stat().st_uid, state.stat().st_uid}
        assert sock.stat().st_mode & 0o777 == 0o660
        assert key.stat().st_mode & 0o777 == 0o600
        control("restart")
        report("0x922")
        wait(lambda: not any(Path(f"/proc/{pid}").exists() for pid in workers), "restart left old children", 15)
        assert journal()["deployments"]["0x921"]["reported"]
        check(f"{profile}: start and restart produced verified reports with preserved journal and split UIDs")

        launcher, workers = pair()
        os.kill(launcher, signal.SIGKILL)
        if manual is not None:
            assert manual.wait(timeout=10) == -signal.SIGKILL
        wait(lambda: not any(Path(f"/proc/{pid}").exists() for pid in workers), "forced launcher crash left children", 15)
        if profile in ("sysv", "manual"):
            control("start")
        report("0x923")
        check(f"{profile}: forced launcher crash removed both children and the replacement signed")

        marker = state / "apply-starts"
        sim.add("0x924", recipe={"lifecycle": {"install": f"echo started >> {marker}; exec sleep 120"}})
        wait(marker.exists, "updater never began", 25)
        _, workers = pair()
        control("stop")
        wait(lambda: not any(Path(f"/proc/{pid}").exists() for pid in workers), "stop left old children", 15)
        wait(lambda: not processes("suifleet-pair") and not sock.exists(), "stop left launcher or socket", 20)
        # Reinstall only the application files, as an integrator would on upgrade.
        command("install", "-m", "0755", "/workspace/suifleet-edge/target/debug/suifleet", "/usr/bin/suifleet")
        command("install", "-m", "0755", "/workspace/scripts/suifleet-portable-signer", LAUNCHER)
        control("start")
        report("0x925")
        assert journal()["deployments"]["0x921"]["reported"]
        outcome = journal()["deployments"]["0x924"]
        assert outcome["indeterminate"] and outcome["status"] is None and not outcome["reported"], outcome
        assert marker.read_text() == "started\n"
        assert key.read_bytes() == key_bytes
        assert not sim.errors, sim.errors
        check(f"{profile}: explicit stop interrupted apply; restart preserved identity and did not replay or report it")
    finally:
        control("stop")
        if runsv is not None:
            runsv.terminate()
            runsv.wait(timeout=10)
