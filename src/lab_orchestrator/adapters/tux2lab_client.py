"""SSH/subprocess adapter to the tux2lab CLI (architecture doc §11).

`Tux2LabClient` is the tux2lab implementation of the generic
`MachineProvider` (adapters/provider.py, architecture doc §18): the base
class validates every dynamic value before delegating to the `_do_*`
methods here, so `FakeTux2LabClient` and `SSHTux2LabClient` enforce
identical input validation, not just matching signatures -- the actual
claim behind M4's done-when ("the fake client passes the same test suite
as the real one"). tux2lab has neither a machine setup nor runs, and no
watchdog: a VM lease keeps the behavior it had before M13.

The data shapes, exceptions and validation moved to provider.py with
M13; the old `Tux2Lab*` names below are aliases of the generic ones, so
`except Tux2LabError` and `except ProviderError` catch the same errors.

**Real CLI dialect (M10).** `SSHTux2LabClient` speaks the host wrapper's
syntax (`deploy/host/lab-orchestrator-wrapper`): tux2lab's own
`vm <cmd> -H <hostname>`, plus `-i <image>` on install, which the wrapper
maps to `-d/-v`. tux2lab prints ANSI-colored text, not JSON, and its errors
go to stdout. Decided (architecture doc §11):

1. `info()` reads `vm list` for the power and OS state, and `vm info -H`
   for the IPv4 address once the VM is running and healthy. `vm info -H`
   alone has no OS state.
2. "Not found" means the hostname is missing from `vm list`. (`vm info -H`
   on an unknown VM prints "State: unknown" and exits 0.)
3. The wrapper passes output through untouched; command errors are read
   from stdout's `[ERROR]` lines plus the exit code.

tux2lab names VMs by FQDN; everything returned here uses the short label
the orchestrator stores (the part before the first dot). The parsers were
written against tux2lab's source and are exercised against
`deploy/host/stand-in/tux2lab`, which reproduces its output; fixtures
captured on the real host are still to come (implementation plan M10).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import replace
from typing import TYPE_CHECKING

from lab_orchestrator.adapters.provider import (
    _HOSTNAME_RE,  # noqa: F401  (re-exported; tests check naming.py against it)
    HOST_CAPACITY_EXIT,
    VM,
    HostCapacityError,
    InvalidIdentifierError,
    MachineProvider,
    ProviderCommandError,
    ProviderConnectionError,
    ProviderError,
    ProviderTimeoutError,
    VMInfo,
    VMNotFoundError,
)
from lab_orchestrator.adapters.ssh_transport import UNSET, SSHTransport, known_hosts_option

if TYPE_CHECKING:
    from lab_orchestrator.core.config import Settings

__all__ = [
    "HOST_CAPACITY_EXIT",
    "VM",
    "FakeTux2LabClient",
    "HostCapacityError",
    "InvalidIdentifierError",
    "SSHTux2LabClient",
    "Tux2LabClient",
    "Tux2LabCommandError",
    "Tux2LabConnectionError",
    "Tux2LabError",
    "Tux2LabTimeoutError",
    "VMInfo",
    "VMNotFoundError",
]

# The pre-M13 names, kept so existing callers and tests read as before.
Tux2LabError = ProviderError
Tux2LabConnectionError = ProviderConnectionError
Tux2LabCommandError = ProviderCommandError
Tux2LabTimeoutError = ProviderTimeoutError


class Tux2LabClient(MachineProvider):
    """The tux2lab provider: lab VMs from golden images. No setup, no
    runs, no watchdog (adapters/provider.py)."""

    name = "tux2lab"


# --- fake client (primary day-to-day development target, per the plan) --


class FakeTux2LabClient(Tux2LabClient):
    """In-memory simulation for local dev and tests without a real KVM
    host (implementation plan §M4) — this is what M5/M6 are meant to
    develop against day to day, unblocked by real-host access.
    """

    def __init__(self, *, simulated_reachability: bool = False) -> None:
        # False (tests' default): the fake IP below is probed for real and
        # is unreachable, which tests use to exercise readiness timeouts.
        # True (LAB_ORCH_TUX2LAB_BACKEND=fake deployments): the probe is
        # skipped, so a simulated VM reaches READY wherever the container
        # runs -- on the VPS 10.28.28.100 is inside the real lab network,
        # which the container can't reach.
        self.simulated_reachability = simulated_reachability
        self._vms: dict[str, VMInfo] = {}
        # Test hook: if set, the *next* _do_* call raises this once, then
        # clears itself. Used to simulate a timeout/ambiguous failure for
        # install_idempotent/remove_idempotent tests without needing a
        # real flaky network.
        self.raise_once: Exception | None = None

    async def _maybe_raise_once(self) -> None:
        if self.raise_once is not None:
            exc, self.raise_once = self.raise_once, None
            raise exc

    async def _do_install(self, hostname: str, image: str) -> None:
        await self._maybe_raise_once()
        if hostname in self._vms:
            raise Tux2LabCommandError(f"VM already exists: {hostname}")
        self._vms[hostname] = VMInfo(
            hostname=hostname, vm_state="stopped", os_state="unknown", ip_address=None
        )

    async def _do_list(self) -> list[VM]:
        await self._maybe_raise_once()
        return [VM(hostname=v.hostname, vm_state=v.vm_state) for v in self._vms.values()]

    async def _do_info(self, hostname: str) -> VMInfo:
        await self._maybe_raise_once()
        try:
            return self._vms[hostname]
        except KeyError:
            raise VMNotFoundError(hostname) from None

    async def _do_start(self, hostname: str) -> None:
        await self._maybe_raise_once()
        try:
            info = self._vms[hostname]
        except KeyError:
            raise VMNotFoundError(hostname) from None
        self._vms[hostname] = replace(info, vm_state="running", os_state="healthy", ip_address="10.28.28.100")

    async def _do_remove(self, hostname: str) -> None:
        await self._maybe_raise_once()
        try:
            del self._vms[hostname]
        except KeyError:
            raise VMNotFoundError(hostname) from None


# --- real client ---------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# A `vm list` row after ANSI stripping: FQDN, VM-State (virsh's first word:
# "running", "shut", "paused", ... or "[ N/A ]"), OS-State ("healthy",
# "SSH-Not-Ready", a systemctl state, or "[ N/A ]"), then the free-text distro.
_VM_LIST_ROW_RE = re.compile(r"^(\S+)\s+(\[ N/A \]|\S+)\s+(\[ N/A \]|\S+)(?:\s+.*)?$")

# An address line under "IPv4" in `vm info -H`'s tree: "a.b.c.d/prefix". The
# gateway line has no prefix length, so it never matches.
_IPV4_WITH_PREFIX_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})/\d{1,2}\b")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _short_label(fqdn: str) -> str:
    """`lab-m01-aurora-7k4m2.hermann.internal` -> `lab-m01-aurora-7k4m2`."""
    return fqdn.split(".", 1)[0]


def _parse_vm_list(raw: str) -> list[tuple[str, str, str]]:
    """`vm list`'s table -> [(short label, vm_state, os_state)]. "shut"
    (virsh's "shut off", cut at the space) is reported as "shut off".
    """
    rows: list[tuple[str, str, str]] = []
    for line in _strip_ansi(raw).splitlines():
        line = line.rstrip()
        if not line or line.startswith("VM-Name") or set(line) == {"-"}:
            continue
        match = _VM_LIST_ROW_RE.match(line)
        if match is None:
            raise Tux2LabCommandError(f"unexpected line in 'vm list' output: {line!r}")
        fqdn, vm_state, os_state = match.groups()
        rows.append((_short_label(fqdn), "shut off" if vm_state == "shut" else vm_state, os_state))
    return rows


def _parse_vm_info_ipv4(raw: str) -> str | None:
    """First IPv4 address in `vm info -H`'s tree, without its prefix length;
    None when the tree has none (VM not running, or SSH not accessible).
    """
    match = _IPV4_WITH_PREFIX_RE.search(_strip_ansi(raw))
    return match.group(1) if match else None


def _error_lines(stdout: str) -> str:
    """tux2lab's `[ERROR] ...` lines (it prints errors to stdout)."""
    lines = [line.strip() for line in _strip_ansi(stdout).splitlines()]
    return " ".join(line for line in lines if line.startswith("[ERROR]"))


class SSHTux2LabClient(Tux2LabClient):
    """Real adapter: SSH to the host-side restricted command wrapper and
    invoke tux2lab's CLI (architecture doc §11). See the module docstring
    for the two real-host assumptions this class has to make.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        client_keys: list[str],
        known_hosts: object = UNSET,
        command_timeout: float = 30.0,
        install_timeout: float = 600.0,
    ) -> None:
        self._ssh = SSHTransport(
            host=host, port=port, username=username, client_keys=client_keys, known_hosts=known_hosts
        )
        self._command_timeout = command_timeout
        self._install_timeout = install_timeout

    @classmethod
    def from_settings(cls, settings: Settings) -> SSHTux2LabClient:
        missing = [
            name
            for name, value in (
                ("tux2lab_ssh_host", settings.tux2lab_ssh_host),
                ("tux2lab_ssh_username", settings.tux2lab_ssh_username),
                ("tux2lab_ssh_key_path", settings.tux2lab_ssh_key_path),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"tux2lab SSH connection settings not configured: {', '.join(missing)} "
                "(set the matching LAB_ORCH_* env vars)"
            )
        return cls(
            host=settings.tux2lab_ssh_host,  # type: ignore[arg-type]
            port=settings.tux2lab_ssh_port,
            username=settings.tux2lab_ssh_username,  # type: ignore[arg-type]
            client_keys=[str(settings.tux2lab_ssh_key_path)],
            known_hosts=known_hosts_option(settings.tux2lab_ssh_known_hosts_path),
            command_timeout=settings.tux2lab_ssh_command_timeout,
            install_timeout=settings.tux2lab_ssh_install_timeout,
        )

    async def close(self) -> None:
        await self._ssh.close()

    async def _run(self, *args: str, timeout: float | None = None) -> str:
        """Run `tux2lab <args...>` over the wrapper connection. `args`
        must already be validated/quoted by the caller. Returns stdout on
        exit_status 0; raises Tux2LabCommandError otherwise, with
        tux2lab's own `[ERROR]` lines (from stdout) or the wrapper's
        refusal (from stderr) in the message.
        """
        command = " ".join(["tux2lab", *args])
        result = await self._ssh.exec(command, timeout=timeout or self._command_timeout)
        if result.exit_status != 0:
            detail = _error_lines(result.stdout) or result.stderr.strip()
            error_cls = (
                HostCapacityError if result.exit_status == HOST_CAPACITY_EXIT else Tux2LabCommandError
            )
            raise error_cls(
                f"'{command}' exited {result.exit_status}" + (f": {detail}" if detail else ""),
                exit_status=result.exit_status,
                stderr=result.stderr,
            )
        return result.stdout

    # Command syntax is the wrapper's (deploy/host/lab-orchestrator-wrapper).
    # shlex.quote() is a second, independent safety layer on top of the
    # charset validation already done by the base class's public methods —
    # not a substitute for it.

    async def _do_install(self, hostname: str, image: str) -> None:
        # Cloning a golden image can outlast the general command timeout.
        await self._run(
            "vm", "install", "-H", shlex.quote(hostname), "-i", shlex.quote(image),
            timeout=self._install_timeout,
        )

    async def _do_list(self) -> list[VM]:
        rows = _parse_vm_list(await self._run("vm", "list"))
        return [VM(hostname=name, vm_state=vm_state) for name, vm_state, _os in rows]

    async def _do_info(self, hostname: str) -> VMInfo:
        rows = _parse_vm_list(await self._run("vm", "list"))
        for name, vm_state, os_state in rows:
            if name == hostname:
                break
        else:
            raise VMNotFoundError(hostname)
        ip_address = None
        if vm_state == "running" and os_state == "healthy":
            ip_address = _parse_vm_info_ipv4(await self._run("vm", "info", "-H", shlex.quote(hostname)))
        return VMInfo(hostname=hostname, vm_state=vm_state, os_state=os_state, ip_address=ip_address)

    async def _do_start(self, hostname: str) -> None:
        # Exits 0 with "VM is already running" after install, which also
        # starts the VM; that is the normal path here.
        await self._run("vm", "start", "-H", shlex.quote(hostname))

    async def _do_remove(self, hostname: str) -> None:
        # The wrapper adds -f. An unknown VM exits 0 ("does not exist"), so
        # check first to keep the base contract (VMNotFoundError).
        if not any(vm.hostname == hostname for vm in await self._do_list()):
            raise VMNotFoundError(hostname)
        await self._run("vm", "remove", "-H", shlex.quote(hostname))
