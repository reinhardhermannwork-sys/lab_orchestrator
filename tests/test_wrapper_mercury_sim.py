"""The mercury-sim branch of the host wrapper (`deploy/host/lab-orchestrator-wrapper`), run directly with the
stand-in CLI (`deploy/host/stand-in/mercury-sim`). sim-backend's runner goes through this wrapper. Only `sudo`
is replaced by a shim.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "deploy" / "host" / "lab-orchestrator-wrapper"
STAND_IN = REPO / "deploy" / "host" / "stand-in" / "mercury-sim"
LEASE = "lab-mrc-mercury-7k4m2"


class _SimHost:
    def __init__(self, workdir: Path) -> None:
        shim_dir = workdir / "bin"
        shim_dir.mkdir()
        sudo = shim_dir / "sudo"
        sudo.write_text('#!/bin/bash\nwhile [[ "$1" != "--" ]]; do shift; done; shift; exec "$@"\n')
        sudo.chmod(0o755)
        self.meminfo = workdir / "meminfo"
        self.loadavg = workdir / "loadavg"
        self.state = workdir / "state.json"
        self.set_host(mem_available_mib=8192, load=0.5)
        conf = workdir / "wrapper.conf"
        conf.write_text(
            f"MERCURY_SIM_USER=claude\nMERCURY_SIM_BIN={STAND_IN}\nMEMINFO={self.meminfo}\n"
            f"LOADAVG={self.loadavg}\nNPROC=4\nRESERVE_MIB=1024\nSIM_MEMORY_MIB=1536\nSIM_CPU=2\n"
        )
        self.env = {
            **os.environ,
            "PATH": f"{shim_dir}:{os.environ.get('PATH', '')}",
            "LAB_ORCHESTRATOR_WRAPPER_CONF": str(conf),
            "MERCURY_SIM_STANDIN_STATE": str(self.state),
        }

    def set_host(self, *, mem_available_mib: int, load: float) -> None:
        self.meminfo.write_text(f"MemTotal: 6000000 kB\nMemAvailable: {mem_available_mib * 1024} kB\n")
        self.loadavg.write_text(f"{load:.2f} 1.00 1.00 2/300 1234\n")

    def run_wrapper(self, command: str) -> tuple[int, str, str]:
        r = subprocess.run(
            [str(WRAPPER)],
            env={**self.env, "SSH_ORIGINAL_COMMAND": command},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        return r.returncode, r.stdout, r.stderr


@pytest.fixture
def sim_host(tmp_path):
    return _SimHost(tmp_path)


def test_a_lease_through_the_wrapper(sim_host):
    for line in (f"mercury-sim install -H {LEASE} -i mercsc0000-v1.2", f"mercury-sim start -H {LEASE}"):
        assert sim_host.run_wrapper(line)[0] == 0
    status, stdout, _ = sim_host.run_wrapper("mercury-sim list")
    assert status == 0 and LEASE in [m["hostname"] for m in json.loads(stdout)]
    assert sim_host.run_wrapper(f"mercury-sim remove -H {LEASE}")[0] == 0


def test_full_host_stops_before_the_cli(sim_host):
    install = f"mercury-sim install -H {LEASE} -i mercsc0000-v1.2"
    sim_host.set_host(mem_available_mib=2000, load=0.5)  # 1536 + 1024 needed
    status, stdout, _ = sim_host.run_wrapper(install)
    assert status == 75 and "out of memory" in stdout
    sim_host.set_host(mem_available_mib=8192, load=2.5)  # 2.5 + 2 > 4 CPUs
    status, stdout, _ = sim_host.run_wrapper(install)
    assert status == 75 and "out of CPU" in stdout
    assert not sim_host.state.exists()  # the CLI was never called
    sim_host.set_host(mem_available_mib=8192, load=1.9)
    assert sim_host.run_wrapper(install)[0] == 0


def test_a_setup_word_gets_through(sim_host):
    word = base64.urlsafe_b64encode(b'{"profile": "di-half"}').decode().rstrip("=")
    line = f"mercury-sim install -H {LEASE} -i mercsc0000-v1.2 --setup-b64 {word}"
    assert sim_host.run_wrapper(line)[0] == 0


@pytest.mark.parametrize(
    "options",
    ["--step 48", "--lot DEMO", "--max-run 300", "--step 0 --max-run 86400", "--step 48 --lot DEMO --max-run 300"],
)
def test_wrapper_lets_run_options_through(sim_host, options):
    status, _stdout, stderr = sim_host.run_wrapper(
        f"mercury-sim run -H lab-x -r PART-DESOX --id 01JBX0000000000000000000A1 {options}"
    )
    assert "refused" not in stderr, stderr
    assert status != 126


@pytest.mark.parametrize(
    "request_line",
    [
        "mercury-sim stop -H lab-x",
        "mercury-sim install -H lab-x -i img --setup {}",
        "mercury-sim install -H lab-x -i img --setup-b64 a+b",
        "mercury-sim install -H lab-x -i img --setup-b64 " + "a" * 22001,  # too long
        "mercury-sim run -H lab-x -r WASHDOWN",
        "mercury-sim run -H lab-x -r WASHDOWN --id 01JBX0000000000000000000A1 --step x",
        "mercury-sim run -H lab-x -r WASHDOWN --id 01JBX0000000000000000000A1 --step",
        "mercury-sim run -H lab-x -r WASHDOWN --id 01JBX0000000000000000000A1 --step 12345",
        "mercury-sim run -H lab-x -r WASHDOWN --id 01JBX0000000000000000000A1 --lot " + "a" * 41,
        "mercury-sim run -H lab-x -r WASHDOWN --id 01JBX0000000000000000000A1 --max-run 0",
        "mercury-sim run -H lab-x -r WASHDOWN --id 01JBX0000000000000000000A1 --max-run 300 --step 2",
        "mercury-sim run -H lab-x -r WASHDOWN --id 01JBX0000000000000000000A1 --step 2 --step 3",
        "mercury-sim run -H lab-x -r WASHDOWN --id 01JBX0000000000000000000A1 --other 2",
        "mercury-sim list -H lab-x",
        "mercury-sim info -H Lab_X",
        "id",
        "mercury-sim info -H lab-x; id",
    ],
)
def test_wrapper_refuses_everything_else(sim_host, request_line):
    status, _stdout, stderr = sim_host.run_wrapper(request_line)
    assert status == 126
    assert "refused" in stderr


def test_no_sim_user_is_refused(sim_host):
    sim_host.env["LAB_ORCHESTRATOR_WRAPPER_CONF"] = "/nonexistent"
    status, _stdout, stderr = sim_host.run_wrapper("mercury-sim list")
    assert status == 126 and "MERCURY_SIM_USER not set" in stderr
