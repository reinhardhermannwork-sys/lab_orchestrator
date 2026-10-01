"""Tests for the tux2lab adapter (M4 done-when criteria, M10 CLI dialect).

The "shared contract" tests below run the *identical* async helper
against both FakeTux2LabClient and SSHTux2LabClient — the latter talking
to a real local SSH server — so "the fake client passes the same test
suite as the real one" (the plan's own words) is demonstrated, not just
asserted.

Behind that SSH server runs the real host wrapper
(`deploy/host/lab-orchestrator-wrapper`) in front of the tux2lab stand-in
(`deploy/host/stand-in/tux2lab`), which reproduces the real CLI's output.
So these tests cover the whole chain: SSH mechanics, the wrapper's
allowlist and image mapping, and the adapter's parsing of tux2lab's text.
Only `sudo` is replaced by a shim that runs the command as the test user.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncssh
import pytest

from lab_orchestrator.adapters.tux2lab_client import (
    FakeTux2LabClient,
    InvalidIdentifierError,
    SSHTux2LabClient,
    Tux2LabClient,
    Tux2LabCommandError,
    Tux2LabTimeoutError,
    VMNotFoundError,
)

# --- local stand-in for the host: wrapper + tux2lab stand-in -------------

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "deploy" / "host" / "lab-orchestrator-wrapper"
STAND_IN = REPO / "deploy" / "host" / "stand-in" / "tux2lab"


class _WrapperHost:
    """Runs each SSH request the way sshd's forced command would: the real
    wrapper with SSH_ORIGINAL_COMMAND set, calling the tux2lab stand-in.
    """

    def __init__(self, workdir: Path) -> None:
        self.received_commands: list[str] = []
        shim_dir = workdir / "bin"
        shim_dir.mkdir()
        sudo = shim_dir / "sudo"
        # Drops "-n -H -u <user> --" and runs the rest as the test user.
        sudo.write_text('#!/bin/bash\nwhile [[ "$1" != "--" ]]; do shift; done; shift; exec "$@"\n')
        sudo.chmod(0o755)
        images = workdir / "images.conf"
        images.write_text("# image distro version\nimage-1 almalinux 10\nmy-image rocky 9\n")
        conf = workdir / "wrapper.conf"
        conf.write_text(f"TUX2LAB_USER=tester\nTUX2LAB_BIN={STAND_IN}\nIMAGES_CONF={images}\n")
        self.env = {
            **os.environ,
            "PATH": f"{shim_dir}:{os.environ.get('PATH', '')}",
            "LAB_ORCHESTRATOR_WRAPPER_CONF": str(conf),
            "TUX2LAB_STANDIN_STATE": str(workdir / "state"),
            "TUX2LAB_STANDIN_BOOT_SECONDS": "0",
            "TUX2LAB_STANDIN_INSTALL_SECONDS": "0",
        }

    async def handle(self, process) -> None:
        self.received_commands.append(process.command)
        if "trigger-timeout" in process.command:
            await asyncio.sleep(5)
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
async def wrapper_server(tmp_path):
    server_state = _WrapperHost(tmp_path)

    host_key = asyncssh.generate_private_key("ssh-ed25519")
    host_key_path = tmp_path / "host_key"
    host_key.write_private_key(str(host_key_path))

    client_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key_path = tmp_path / "client_key"
    client_key.write_private_key(str(client_key_path))
    authorized_keys_path = tmp_path / "authorized_keys"
    authorized_keys_path.write_bytes(client_key.export_public_key())

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_host_keys=[str(host_key_path)],
        authorized_client_keys=str(authorized_keys_path),
        process_factory=server_state.handle,
    )
    port = server.sockets[0].getsockname()[1]

    yield server_state, port, str(client_key_path)

    server.close()
    await server.wait_closed()


@pytest.fixture
async def ssh_client(wrapper_server):
    _server_state, port, client_key_path = wrapper_server
    client = SSHTux2LabClient(
        host="127.0.0.1",
        port=port,
        username="orchestrator",
        client_keys=[client_key_path],
        known_hosts=None,  # test-only: real deployment must not do this (see module docstring)
        command_timeout=1.0,
    )
    yield client
    await client.close()


# --- shared behavioral contract -------------------------------------------


async def _assert_full_lifecycle_contract(client: Tux2LabClient) -> None:
    """Behavior every Tux2LabClient implementation must satisfy."""
    with pytest.raises(VMNotFoundError):
        await client.info("no-such-host")

    await client.install("test-host-1", "image-1")
    listed = await client.list()
    assert any(vm.hostname == "test-host-1" for vm in listed)

    info = await client.info("test-host-1")
    assert info.hostname == "test-host-1"
    # The fake installs a stopped VM; the real CLI's install also starts it.
    assert info.vm_state in ("stopped", "running")

    await client.start("test-host-1")
    info_after_start = await client.info("test-host-1")
    assert info_after_start.vm_state == "running"
    assert info_after_start.os_state == "healthy"
    assert info_after_start.ip_address

    await client.remove("test-host-1")
    with pytest.raises(VMNotFoundError):
        await client.info("test-host-1")


async def test_fake_client_satisfies_lifecycle_contract():
    await _assert_full_lifecycle_contract(FakeTux2LabClient())


async def test_ssh_client_satisfies_lifecycle_contract(ssh_client):
    await _assert_full_lifecycle_contract(ssh_client)


# --- injection safety (M4 done-when: rejected before reaching the SSH call) -


@pytest.mark.parametrize(
    "bad_hostname",
    [
        "host; rm -rf /",
        "../../etc/passwd",
        "host$(whoami)",
        "HOST-UPPERCASE",
        "-leading-hyphen",
        "host with spaces",
        "",
    ],
)
async def test_malicious_or_malformed_hostnames_rejected_by_fake_client(bad_hostname):
    client = FakeTux2LabClient()
    with pytest.raises(InvalidIdentifierError):
        await client.install(bad_hostname, "image-1")


async def test_malicious_hostname_never_reaches_the_ssh_call(ssh_client, wrapper_server):
    server_state, _port, _key = wrapper_server
    with pytest.raises(InvalidIdentifierError):
        await ssh_client.install("host; rm -rf /", "image-1")
    assert server_state.received_commands == []


async def test_malicious_image_name_never_reaches_the_ssh_call(ssh_client, wrapper_server):
    server_state, _port, _key = wrapper_server
    with pytest.raises(InvalidIdentifierError):
        await ssh_client.install("valid-host", "image; rm -rf /")
    assert server_state.received_commands == []


# --- SSH-specific: timeout and command-error handling ---------------------


async def test_timeout_raises_tux2lab_timeout_error(ssh_client):
    # start uses the general command timeout (1s here); install has its own.
    with pytest.raises(Tux2LabTimeoutError):
        await ssh_client.start("trigger-timeout")


async def test_command_error_carries_tux2lab_error_from_stdout(ssh_client):
    # tux2lab prints errors to stdout; the message must carry them.
    await ssh_client.install("dup-host", "image-1")
    with pytest.raises(Tux2LabCommandError) as exc_info:
        await ssh_client.install("dup-host", "image-1")
    assert exc_info.value.exit_status == 1
    assert 'VM "dup-host.lab.internal" exists already.' in str(exc_info.value)


async def test_wrapper_refusal_surfaces_as_command_error(ssh_client):
    with pytest.raises(Tux2LabCommandError) as exc_info:
        await ssh_client.install("some-host", "image-not-in-map")
    assert exc_info.value.exit_status == 126
    assert "image not in map" in str(exc_info.value)


async def test_remove_of_unknown_vm_raises_not_found(ssh_client):
    # The real CLI exits 0 here; the adapter keeps the base contract.
    with pytest.raises(VMNotFoundError):
        await ssh_client.remove("never-created")


async def test_install_uses_the_longer_install_timeout(wrapper_server):
    _server_state, port, client_key_path = wrapper_server
    client = SSHTux2LabClient(
        host="127.0.0.1",
        port=port,
        username="orchestrator",
        client_keys=[client_key_path],
        known_hosts=None,
        command_timeout=0.1,
        install_timeout=10.0,
    )
    try:
        await client.install("slow-ok-host", "image-1")  # well over 0.1s through bash
    finally:
        await client.close()


async def test_ssh_client_sends_expected_command_syntax(ssh_client, wrapper_server):
    server_state, _port, _key = wrapper_server
    await ssh_client.install("my-host", "my-image")
    await ssh_client.info("my-host")
    assert server_state.received_commands == [
        "tux2lab vm install -H my-host -i my-image",
        "tux2lab vm list",
        "tux2lab vm info -H my-host",
    ]


# --- idempotent retry helpers (architecture doc §14.4) ---------------------


async def test_install_idempotent_propagates_a_genuine_failure():
    client = FakeTux2LabClient()
    client.raise_once = Tux2LabTimeoutError("simulated")
    # The underlying install() never actually ran, so info() correctly
    # says the host doesn't exist -- this must surface as a real failure,
    # not be silently swallowed.
    with pytest.raises(Tux2LabTimeoutError):
        await client.install_idempotent("host-x", "image-1")


async def test_install_idempotent_recovers_when_install_actually_succeeded():
    client = FakeTux2LabClient()
    await client.install("host-y", "image-1")  # really happened
    # Simulate: a retry believes the prior attempt timed out, but the VM
    # is already there (the response was merely lost).
    client.raise_once = Tux2LabTimeoutError("simulated")
    await client.install_idempotent("host-y", "image-1")  # must not raise


async def test_remove_idempotent_recovers_when_already_gone():
    client = FakeTux2LabClient()
    await client.install("host-z", "image-1")
    del client._vms["host-z"]  # simulate: a prior remove() actually succeeded
    client.raise_once = Tux2LabTimeoutError("simulated")
    await client.remove_idempotent("host-z")  # must not raise


async def test_remove_idempotent_propagates_real_timeout_when_still_exists():
    client = FakeTux2LabClient()
    await client.install("host-w", "image-1")
    client.raise_once = Tux2LabTimeoutError("simulated")
    with pytest.raises(Tux2LabTimeoutError):
        await client.remove_idempotent("host-w")  # still exists -> real failure
