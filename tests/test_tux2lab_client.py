"""Tests for the tux2lab adapter (M4 done-when criteria).

The "shared contract" tests below run the *identical* async helper
against both FakeTux2LabClient and SSHTux2LabClient — the latter talking
to a real local SSH server standing in for the host-side wrapper — so
"the fake client passes the same test suite as the real one" (the plan's
own words) is demonstrated, not just asserted.

The local wrapper server mirrors SSHTux2LabClient's *assumed* command
syntax and output format (both flagged as unverified in
adapters/tux2lab_client.py's module docstring). That means these tests
genuinely verify the SSH mechanics — auth, command execution, timeout
handling, error propagation — even though the wrapper's actual CLI
dialect can't be confirmed without a real host.
"""

from __future__ import annotations

import asyncio
import json
import shlex

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

# --- local stand-in for the host-side wrapper ----------------------------


class _FakeWrapperServer:
    """Responds to exactly the command syntax SSHTux2LabClient sends,
    with the JSON output format it expects — see the module docstring.
    """

    def __init__(self) -> None:
        self.vms: dict[str, dict] = {}
        self.received_commands: list[str] = []

    async def handle(self, process) -> None:
        self.received_commands.append(process.command)
        args = shlex.split(process.command)  # ["tux2lab", "vm", "install", "-H", ...]
        op = tuple(args[1:3])
        hostname = self._flag(args, "-H")

        if op == ("vm", "install"):
            if hostname == "trigger-timeout":
                await asyncio.sleep(5)
            image = self._flag(args, "-i")
            if hostname in self.vms:
                process.stderr.write("already exists")
                process.exit(1)
                return
            self.vms[hostname] = {
                "hostname": hostname,
                "vm_state": "stopped",
                "os_state": "unknown",
                "ip_address": None,
                "image": image,
            }
            process.exit(0)
        elif op == ("vm", "list"):
            process.stdout.write(json.dumps(list(self.vms.values())))
            process.exit(0)
        elif op == ("vm", "info"):
            vm = self.vms.get(hostname)
            if vm is None:
                process.stderr.write(f"host not found: {hostname}")
                process.exit(1)
                return
            process.stdout.write(json.dumps(vm))
            process.exit(0)
        elif op == ("vm", "start"):
            vm = self.vms.get(hostname)
            if vm is None:
                process.stderr.write(f"host not found: {hostname}")
                process.exit(1)
                return
            vm.update(vm_state="running", os_state="healthy", ip_address="10.28.28.100")
            process.exit(0)
        elif op == ("vm", "remove"):
            if hostname not in self.vms:
                process.stderr.write(f"host not found: {hostname}")
                process.exit(1)
                return
            del self.vms[hostname]
            process.exit(0)
        else:
            process.stderr.write("unknown command")
            process.exit(2)

    @staticmethod
    def _flag(args: list[str], name: str) -> str | None:
        try:
            return args[args.index(name) + 1]
        except ValueError:
            return None


@pytest.fixture
async def wrapper_server(tmp_path):
    server_state = _FakeWrapperServer()

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
    assert info.vm_state == "stopped"

    await client.start("test-host-1")
    info_after_start = await client.info("test-host-1")
    assert info_after_start.vm_state == "running"
    assert info_after_start.os_state == "healthy"

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
    with pytest.raises(Tux2LabTimeoutError):
        await ssh_client.install("trigger-timeout", "image-1")


async def test_command_error_carries_exit_status_and_stderr(ssh_client):
    await ssh_client.install("dup-host", "image-1")
    with pytest.raises(Tux2LabCommandError) as exc_info:
        await ssh_client.install("dup-host", "image-1")
    assert exc_info.value.exit_status == 1
    assert "already exists" in exc_info.value.stderr


async def test_ssh_client_sends_expected_command_syntax(ssh_client, wrapper_server):
    server_state, _port, _key = wrapper_server
    await ssh_client.install("my-host", "my-image")
    assert server_state.received_commands == ["tux2lab vm install -H my-host -i my-image"]


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
