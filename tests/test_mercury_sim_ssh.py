"""Tests for M13 SP3: `SSHMercurySimClient` -> a real local SSH server ->
the real host wrapper (`deploy/host/lab-orchestrator-wrapper`, mercury-sim
branch) -> the mercury-sim stand-in (`deploy/host/stand-in/mercury-sim`).
As in test_tux2lab_client.py, only `sudo` is replaced by a shim.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import asyncssh
import pytest
from ulid import ULID

from lab_orchestrator.adapters.mercury_sim import SSHMercurySimClient, setup_b64
from lab_orchestrator.adapters.provider import (
    HostCapacityError,
    InvalidIdentifierError,
    ProviderCommandError,
    RunRefusedError,
    VMNotFoundError,
)

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "deploy" / "host" / "lab-orchestrator-wrapper"
STAND_IN = REPO / "deploy" / "host" / "stand-in" / "mercury-sim"
LEASE = "lab-mrc-mercury-7k4m2"


class _SimHost:
    def __init__(self, workdir: Path) -> None:
        self.received: list[str] = []
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

    async def handle(self, process) -> None:
        self.received.append(process.command)
        proc = await asyncio.create_subprocess_exec(
            str(WRAPPER),
            env={**self.env, "SSH_ORIGINAL_COMMAND": process.command},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        process.stdout.write(stdout.decode())
        process.stderr.write(stderr.decode())
        process.exit(proc.returncode)


@pytest.fixture
async def sim_host(tmp_path):
    host = _SimHost(tmp_path)
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    host_key.write_private_key(str(tmp_path / "host_key"))
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key.write_private_key(str(tmp_path / "client_key"))
    (tmp_path / "authorized_keys").write_bytes(client_key.export_public_key())
    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_host_keys=[str(tmp_path / "host_key")],
        authorized_client_keys=str(tmp_path / "authorized_keys"),
        process_factory=host.handle,
    )
    client = SSHMercurySimClient(
        host="127.0.0.1",
        port=server.sockets[0].getsockname()[1],
        username="lab-orchestrator",
        client_keys=[str(tmp_path / "client_key")],
        known_hosts=None,  # test-only
        command_timeout=5.0,
    )
    yield host, client
    await client.close()
    server.close()
    await server.wait_closed()


async def test_lease_and_runs_through_ssh_and_the_wrapper(sim_host):
    host, client = sim_host
    with pytest.raises(VMNotFoundError):
        await client.info(LEASE)
    await client.install(LEASE, "mercsc0000-v1.2", {"profile": "di-half", "changes": []})
    stored = json.loads(host.state.read_text())[LEASE]
    assert stored["setup"] == {"changes": [], "profile": "di-half"}
    assert stored["image"] == "mercsc0000-v1.2"

    await client.start(LEASE)
    info = await client.info(LEASE)
    assert (info.vm_state, info.os_state, info.ip_address) == ("running", "healthy", LEASE)
    assert [vm.hostname for vm in await client.list()] == [LEASE]

    run_id = str(ULID())
    run = await client.run(LEASE, run_id, "WASHDOWN")
    assert (run.run_id, run.state) == (run_id, "queued")
    with pytest.raises(RunRefusedError, match="run going"):
        await client.run(LEASE, str(ULID()), "WASHDOWN")
    assert [r.state for r in await client.runs(LEASE)] == ["running"]
    (done,) = await client.runs(LEASE)
    assert (done.state, done.result, done.recipe) == ("done", "PROCESS_DONE", "WASHDOWN")
    assert done.blobs == ["sbmcy/Log/PROCESSCONTROL/639290886208180982.blob"]

    await client.remove(LEASE)
    with pytest.raises(VMNotFoundError):
        await client.remove(LEASE)
    with pytest.raises(VMNotFoundError):
        await client.runs(LEASE)


async def test_full_host_is_a_capacity_error(sim_host):
    host, client = sim_host
    host.set_host(mem_available_mib=2000, load=0.5)  # 1536 + 1024 needed
    with pytest.raises(HostCapacityError, match="out of memory"):
        await client.install(LEASE, "mercsc0000-v1.2")
    host.set_host(mem_available_mib=8192, load=2.5)  # 2.5 + 2 > 4 CPUs
    with pytest.raises(HostCapacityError, match="out of CPU"):
        await client.install(LEASE, "mercsc0000-v1.2")
    assert not host.state.exists()  # the CLI was never called
    host.set_host(mem_available_mib=8192, load=1.9)
    await client.install(LEASE, "mercsc0000-v1.2")


async def test_bad_values_never_reach_ssh(sim_host):
    host, client = sim_host
    for call in (
        client.install("Bad;name", "mercsc0000-v1.2"),
        client.install(LEASE, "img$(id)"),
        client.run(LEASE, str(ULID()), "WASH DOWN"),
        client.run(LEASE, "not-a-ulid", "WASHDOWN"),
    ):
        with pytest.raises(InvalidIdentifierError):
            await call
    assert host.received == []


@pytest.mark.parametrize(
    "request_line",
    [
        "mercury-sim stop -H lab-x",
        "mercury-sim install -H lab-x -i img --setup {}",
        "mercury-sim install -H lab-x -i img --setup-b64 a+b",
        "mercury-sim install -H lab-x -i img --setup-b64 " + "a" * 22001,  # too long
        "mercury-sim run -H lab-x -r WASHDOWN",
        "mercury-sim run -H lab-x -r WASHDOWN --id 01JBX0000000000000000000A1 --step 2",
        "mercury-sim list -H lab-x",
        "mercury-sim info -H Lab_X",
        "id",
        "mercury-sim info -H lab-x; id",
    ],
)
def test_wrapper_refuses_everything_else(sim_host, request_line):
    host, _client = sim_host
    status, _stdout, stderr = host.run_wrapper(request_line)
    assert status == 126
    assert "refused" in stderr


async def test_wrapper_refusal_is_a_command_error(sim_host):
    host, client = sim_host
    host.env["LAB_ORCHESTRATOR_WRAPPER_CONF"] = "/nonexistent"  # MERCURY_SIM_USER unset
    with pytest.raises(ProviderCommandError, match="MERCURY_SIM_USER not set"):
        await client.list()


def test_setup_b64_is_a_single_wrapper_safe_word():
    word = setup_b64('{"a":"b c","d":[1,2]}' * 50)
    assert word and all(c.isalnum() or c in "-_" for c in word)
