"""SSH/subprocess adapter to the tux2lab CLI (architecture doc §11).

`Tux2LabClient` is an abstract base class matching the architecture doc's
five-method interface exactly (`install`, `list`, `info`, `start`,
`remove`). Its *public* methods validate every dynamic value against a
strict allowed-charset before delegating to a subclass-implemented
`_do_*` method — this guarantees `FakeTux2LabClient` and
`SSHTux2LabClient` enforce identical input validation, not just matching
signatures, which is the actual claim behind M4's done-when ("the fake
client passes the same test suite as the real one").

Two more things live on the base class, built on top of the public
(validated) methods rather than duplicated per subclass:

- `install_idempotent` / `remove_idempotent` — safe to call after a prior
  attempt that may or may not have succeeded (architecture doc §14.4: a
  timeout with no definitive success/failure signal). They check `info()`
  for a VM already in a post-operation state before treating a fresh
  `Tux2LabTimeoutError` as a real failure, instead of blindly reissuing a
  mutating call into a double-provision. instance_manager.py (M5) should
  call these, not the raw `install`/`remove`, for exactly this reason.

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
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncssh

    from lab_orchestrator.core.config import Settings

# --- data shapes -----------------------------------------------------


@dataclass(frozen=True)
class VM:
    """One VM as reported by `tux2lab vm list`."""

    hostname: str
    vm_state: str  # e.g. "running", "stopped"


@dataclass(frozen=True)
class VMInfo:
    """One VM's detail as reported by `tux2lab vm info -H <hostname>`.

    TCP/22 reachability (part of M5's readiness criteria alongside
    vm_state/os_state) is deliberately *not* a field here — tux2lab has
    no way to know how reachable a VM is from the orchestrator
    container's network position. That check belongs in M5, as a plain
    TCP-connect attempt, not in this adapter.
    """

    hostname: str
    vm_state: str  # e.g. "running", "stopped", "error"
    os_state: str  # e.g. "healthy", "booting", "unknown" — tux2lab's own guest-agent signal
    ip_address: str | None


# --- exceptions --------------------------------------------------------


class Tux2LabError(Exception):
    """Base class for every error this adapter raises."""


class Tux2LabConnectionError(Tux2LabError):
    """Could not reach the host-side wrapper at all (SSH connect failed)."""


class Tux2LabCommandError(Tux2LabError):
    """The wrapper/CLI ran and returned a clear non-zero exit."""

    def __init__(self, message: str, *, exit_status: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.exit_status = exit_status
        self.stderr = stderr


class Tux2LabTimeoutError(Tux2LabError):
    """The call timed out with no definitive success/failure signal — the
    specific "ambiguous" case architecture doc §14.4 calls out. Callers
    should check `info()`/`list()` before retrying (see
    `install_idempotent`/`remove_idempotent`) rather than blindly
    reissuing a mutating call.
    """


class VMNotFoundError(Tux2LabError):
    """`info()` (or an operation that implies it) was called for a
    hostname tux2lab doesn't know about.
    """


class InvalidIdentifierError(Tux2LabError, ValueError):
    """A hostname or image name failed the allowed-charset check before
    it could cross the SSH boundary (architecture doc §14.3). Also a
    `ValueError` so ordinary `except ValueError` handling still catches
    it, matching core/config.py's `MachineConfigError` convention.
    """


# --- input validation (architecture doc §14.3) --------------------------

# RFC 1123 hostname-label shape: lowercase alnum, hyphens, not leading or
# trailing with one, max 63 chars. Matches what naming.py produces (a
# single opaque label, no DNS suffix -- architecture doc §5).
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# tux2lab image names as seen in config/machines.yaml (e.g.
# "image_1_software_1") — alnum, underscore, hyphen, dot.
_IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _validate_hostname(hostname: str) -> None:
    if not _HOSTNAME_RE.match(hostname):
        raise InvalidIdentifierError(f"invalid hostname (fails allowed-charset check): {hostname!r}")


def _validate_image_name(image: str) -> None:
    if not _IMAGE_NAME_RE.match(image):
        raise InvalidIdentifierError(f"invalid image name (fails allowed-charset check): {image!r}")


# --- the adapter interface ---------------------------------------------


class Tux2LabClient(ABC):
    """Matches architecture doc §11's adapter boundary exactly:
    `install`, `list`, `info`, `start`, `remove`. Subclasses implement
    the `_do_*` methods; the public methods above them handle input
    validation so it's identical across every implementation.
    """

    #: True only for a simulation whose VMs exist nowhere on the network:
    #: the orchestrator then skips its own TCP/22 readiness probe
    #: (instance_manager), which could never succeed against them.
    simulated_reachability: bool = False

    async def install(self, hostname: str, image: str) -> None:
        _validate_hostname(hostname)
        _validate_image_name(image)
        await self._do_install(hostname, image)

    async def list(self) -> list[VM]:
        return await self._do_list()

    async def info(self, hostname: str) -> VMInfo:
        _validate_hostname(hostname)
        return await self._do_info(hostname)

    async def start(self, hostname: str) -> None:
        _validate_hostname(hostname)
        await self._do_start(hostname)

    async def remove(self, hostname: str) -> None:
        _validate_hostname(hostname)
        await self._do_remove(hostname)

    @abstractmethod
    async def _do_install(self, hostname: str, image: str) -> None: ...

    @abstractmethod
    async def _do_list(self) -> list[VM]: ...

    @abstractmethod
    async def _do_info(self, hostname: str) -> VMInfo: ...

    @abstractmethod
    async def _do_start(self, hostname: str) -> None: ...

    @abstractmethod
    async def _do_remove(self, hostname: str) -> None: ...

    async def close(self) -> None:
        """Release any held resources (e.g. an SSH connection). Safe
        no-op by default — only `SSHTux2LabClient` actually holds
        anything to close. Lets main.py call this uniformly at shutdown
        regardless of which concrete client is wired in.
        """

    async def install_idempotent(self, hostname: str, image: str) -> None:
        """Like `install()`, but safe to call again after a prior attempt
        whose outcome is unknown. See the module docstring and
        architecture doc §14.4.
        """
        try:
            await self.install(hostname, image)
        except Tux2LabTimeoutError as timeout_exc:
            try:
                await self.info(hostname)
            except VMNotFoundError:
                # Genuinely didn't happen — the timeout was a real failure.
                raise timeout_exc from None
            # info() succeeded: the install evidently did happen despite
            # the timeout. Treat this call as an (idempotent) success.
            return

    async def remove_idempotent(self, hostname: str) -> None:
        """Like `remove()`, but safe to call again after a prior attempt
        whose outcome is unknown. See the module docstring and
        architecture doc §14.4.
        """
        try:
            await self.remove(hostname)
        except Tux2LabTimeoutError as timeout_exc:
            try:
                await self.info(hostname)
            except VMNotFoundError:
                # Gone — the remove evidently did happen despite the timeout.
                return
            # Still exists: the remove really didn't happen.
            raise timeout_exc from None


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

# Sentinel distinguishing "don't pass known_hosts at all" (asyncssh's own
# default: check against ~/.ssh/known_hosts, fail closed if there's no
# entry) from an explicit `known_hosts=None` (which *disables* host-key
# checking entirely — a real security downgrade, never the default here).
_UNSET = object()

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
        known_hosts: object = _UNSET,
        command_timeout: float = 30.0,
        install_timeout: float = 600.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._client_keys = client_keys
        self._known_hosts = known_hosts
        self._command_timeout = command_timeout
        self._install_timeout = install_timeout
        self._conn: asyncssh.SSHClientConnection | None = None

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
        known_hosts: object = _UNSET
        if settings.tux2lab_ssh_known_hosts_path is not None:
            known_hosts = str(settings.tux2lab_ssh_known_hosts_path)
        return cls(
            host=settings.tux2lab_ssh_host,  # type: ignore[arg-type]
            port=settings.tux2lab_ssh_port,
            username=settings.tux2lab_ssh_username,  # type: ignore[arg-type]
            client_keys=[str(settings.tux2lab_ssh_key_path)],
            known_hosts=known_hosts,
            command_timeout=settings.tux2lab_ssh_command_timeout,
            install_timeout=settings.tux2lab_ssh_install_timeout,
        )

    async def _connection(self) -> asyncssh.SSHClientConnection:
        import asyncssh

        if self._conn is None or self._conn.is_closed():
            connect_kwargs: dict = {
                "host": self._host,
                "port": self._port,
                "username": self._username,
                "client_keys": self._client_keys,
            }
            if self._known_hosts is not _UNSET:
                connect_kwargs["known_hosts"] = self._known_hosts
            try:
                self._conn = await asyncssh.connect(**connect_kwargs)
            except (OSError, asyncssh.Error) as exc:
                raise Tux2LabConnectionError(str(exc)) from exc
        return self._conn

    async def close(self) -> None:
        if self._conn is not None and not self._conn.is_closed():
            self._conn.close()
            await self._conn.wait_closed()

    async def _run(self, *args: str, timeout: float | None = None) -> str:
        """Run `tux2lab <args...>` over the wrapper connection. `args`
        must already be validated/quoted by the caller. Returns stdout on
        exit_status 0; raises Tux2LabCommandError otherwise, with
        tux2lab's own `[ERROR]` lines (from stdout) or the wrapper's
        refusal (from stderr) in the message.
        """
        import asyncssh

        conn = await self._connection()
        command = " ".join(["tux2lab", *args])
        try:
            result = await conn.run(command, check=False, timeout=timeout or self._command_timeout)
        except asyncssh.TimeoutError as exc:
            raise Tux2LabTimeoutError(command) from exc
        except asyncssh.Error as exc:
            raise Tux2LabConnectionError(str(exc)) from exc
        stdout = str(result.stdout or "")
        if result.exit_status != 0:
            stderr = str(result.stderr or "")
            detail = _error_lines(stdout) or stderr.strip()
            raise Tux2LabCommandError(
                f"'{command}' exited {result.exit_status}" + (f": {detail}" if detail else ""),
                exit_status=result.exit_status,
                stderr=stderr,
            )
        return stdout

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
