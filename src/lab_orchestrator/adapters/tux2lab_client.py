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

**Real-host assumptions.** `SSHTux2LabClient` has to guess at two things
the architecture doc doesn't specify, because the host-side wrapper
doesn't exist yet for this codebase to inspect. Both are called out again
on that class, but worth flagging here too:

1. The wrapper's exact command syntax (assumed to mirror tux2lab's own
   CLI, `-H <hostname>` etc., since that's the only syntax documented).
2. `vm list`/`vm info`'s output format (assumed JSON) and how a
   "VM not found" condition is signaled (a stderr-text heuristic, since
   there's no documented distinct signal).

Neither assumption is exercised by `FakeTux2LabClient`, which is what
M5/M6 develop against day to day per the implementation plan. Both are
covered by `tests/test_tux2lab_client.py` against a real local SSH
server standing in for the wrapper — so the SSH *mechanics* (auth,
command execution, timeout/error handling) are genuinely verified, only
the wrapper's specific CLI dialect remains unverified pending real-host
access.
"""

from __future__ import annotations

import json
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
# trailing with one, max 63 chars. Matches what naming.py (M5) will
# produce, once §5's DNS-suffix question is settled.
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

    def __init__(self) -> None:
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

_NOT_FOUND_STDERR_PATTERNS = ("not found", "does not exist", "no such", "unknown host")


def _parse_vm_list_json(raw: str) -> list[VM]:
    """ASSUMED format (unverified — see module docstring): a JSON array
    of objects with "hostname" and "vm_state" keys.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Tux2LabCommandError(f"could not parse 'vm list' output as JSON: {exc}") from exc
    return [VM(hostname=entry["hostname"], vm_state=entry["vm_state"]) for entry in data]


def _parse_vm_info_json(raw: str) -> VMInfo:
    """ASSUMED format (unverified — see module docstring): a single JSON
    object with "hostname", "vm_state", "os_state", and nullable
    "ip_address" keys.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Tux2LabCommandError(f"could not parse 'vm info' output as JSON: {exc}") from exc
    return VMInfo(
        hostname=data["hostname"],
        vm_state=data["vm_state"],
        os_state=data["os_state"],
        ip_address=data.get("ip_address"),
    )


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
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._client_keys = client_keys
        self._known_hosts = known_hosts
        self._command_timeout = command_timeout
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

    async def _run(self, *args: str) -> str:
        """Run `tux2lab <args...>` over the wrapper connection. `args`
        must already be validated/quoted by the caller. Returns stdout on
        exit_status 0; raises Tux2LabCommandError otherwise.
        """
        import asyncssh

        conn = await self._connection()
        command = " ".join(["tux2lab", *args])
        try:
            result = await conn.run(command, check=False, timeout=self._command_timeout)
        except asyncssh.TimeoutError as exc:
            raise Tux2LabTimeoutError(command) from exc
        except asyncssh.Error as exc:
            raise Tux2LabConnectionError(str(exc)) from exc
        if result.exit_status != 0:
            raise Tux2LabCommandError(
                f"'{command}' exited {result.exit_status}",
                exit_status=result.exit_status,
                stderr=str(result.stderr or ""),
            )
        return str(result.stdout or "")

    # --- ASSUMPTION: wrapper mirrors tux2lab's own CLI syntax exactly,
    # `-H <hostname>` per §11's info/start/remove examples; install's
    # flags are elided with "..." in the doc, so `-H`/`-i` here is this
    # module's own extrapolation of that convention. shlex.quote() is a
    # second, independent safety layer on top of the charset validation
    # already done by the base class's public methods — not a substitute
    # for it.

    async def _do_install(self, hostname: str, image: str) -> None:
        await self._run("vm", "install", "-H", shlex.quote(hostname), "-i", shlex.quote(image))

    async def _do_list(self) -> list[VM]:
        raw = await self._run("vm", "list")
        return _parse_vm_list_json(raw)

    async def _do_info(self, hostname: str) -> VMInfo:
        try:
            raw = await self._run("vm", "info", "-H", shlex.quote(hostname))
        except Tux2LabCommandError as exc:
            # Best-effort heuristic on stderr text — there's no documented
            # distinct signal for "not found" vs. any other command
            # failure. A stderr that doesn't match propagates as
            # Tux2LabCommandError, not VMNotFoundError: install_idempotent/
            # remove_idempotent's retry-safety may not reliably detect
            # "gone"/"not yet created" against a real host until this is
            # confirmed against the actual wrapper. Fully reliable against
            # FakeTux2LabClient today.
            if any(pattern in exc.stderr.lower() for pattern in _NOT_FOUND_STDERR_PATTERNS):
                raise VMNotFoundError(hostname) from exc
            raise
        return _parse_vm_info_json(raw)

    async def _do_start(self, hostname: str) -> None:
        await self._run("vm", "start", "-H", shlex.quote(hostname))

    async def _do_remove(self, hostname: str) -> None:
        await self._run("vm", "remove", "-H", shlex.quote(hostname))
