"""The generic machine provider (architecture doc §11, §18).

A provider builds, starts, reports on and removes the machine behind a
lease: `tux2lab` makes VMs (adapters/tux2lab_client.py). The orchestrator
keeps one client per provider in a `ProviderRegistry`; the instance
manager, janitor and reconcile take the client from the lease's
`provider` column.

`MachineProvider` is the old §11 `Tux2LabClient` interface, generalised:
its public methods validate every dynamic value against a strict
allowed-charset before delegating to a subclass-implemented `_do_*`
method, so every implementation enforces identical input validation
(§14.3).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Literal

# Every provider name the orchestrator knows. machines.yaml's `provider`
# and the per-provider limit triggers (db/models.py) both use this list.
ProviderName = Literal["tux2lab"]
PROVIDERS: tuple[str, ...] = ("tux2lab",)

# --- data shapes -----------------------------------------------------


@dataclass(frozen=True)
class VM:
    """One machine as reported by the provider's `list`."""

    hostname: str
    vm_state: str  # e.g. "running", "stopped"


@dataclass(frozen=True)
class VMInfo:
    """One machine's detail as reported by the provider's `info`.

    Reachability from the orchestrator (part of M5's readiness criteria
    alongside vm_state/os_state) is deliberately *not* a field here --
    the provider has no way to know how reachable a machine is from the
    orchestrator container's network position. That check belongs in the
    instance manager, as a plain TCP-connect attempt on the machine's
    `probe_port`.
    """

    hostname: str
    vm_state: str  # e.g. "running", "stopped", "error", "exited"
    os_state: str  # e.g. "healthy", "booting", "unknown", "link-lost"
    ip_address: str | None


# --- exceptions --------------------------------------------------------


class ProviderError(Exception):
    """Base class for every error a provider raises."""


class ProviderConnectionError(ProviderError):
    """Could not reach the host-side wrapper at all (SSH connect failed)."""


class ProviderCommandError(ProviderError):
    """The wrapper/CLI ran and returned a clear non-zero exit."""

    def __init__(self, message: str, *, exit_status: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.exit_status = exit_status
        self.stderr = stderr


class HostCapacityError(ProviderCommandError):
    """The host wrapper refused an install because the host lacks free
    RAM (or CPU) for the machine plus its reserve (exit 75, EX_TEMPFAIL;
    architecture doc §11). Definitive: no machine was created, so it is
    never retried.
    """


# Exit code the wrapper uses for "host out of capacity" (EX_TEMPFAIL).
HOST_CAPACITY_EXIT = 75


class ProviderTimeoutError(ProviderError):
    """The call timed out with no definitive success/failure signal -- the
    specific "ambiguous" case architecture doc §14.4 calls out. Callers
    should check `info()`/`list()` before retrying (see
    `install_idempotent`/`remove_idempotent`) rather than blindly
    reissuing a mutating call.
    """


class VMNotFoundError(ProviderError):
    """`info()` (or an operation that implies it) was called for a
    hostname the provider doesn't know about.
    """


class InvalidIdentifierError(ProviderError, ValueError):
    """A hostname, image, recipe, run id or setup failed its check before
    it could cross the SSH boundary (architecture doc §14.3). Also a
    `ValueError` so ordinary `except ValueError` handling still catches
    it, matching core/config.py's `MachineConfigError` convention.
    """


# --- input validation (architecture doc §14.3) --------------------------

# RFC 1123 hostname-label shape: lowercase alnum, hyphens, not leading or
# trailing with one, max 63 chars. Matches what naming.py produces (a
# single opaque label, no DNS suffix -- architecture doc §5).
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# Image names as seen in config/machines.yaml (e.g. "image_1_software_1",
# "image_2_software_2") -- alnum, underscore, hyphen, dot.
_IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")



def validate_hostname(hostname: str) -> None:
    if not _HOSTNAME_RE.match(hostname):
        raise InvalidIdentifierError(f"invalid hostname (fails allowed-charset check): {hostname!r}")


def validate_image_name(image: str) -> None:
    if not _IMAGE_NAME_RE.match(image):
        raise InvalidIdentifierError(f"invalid image name (fails allowed-charset check): {image!r}")


# --- the provider interface --------------------------------------------


class MachineProvider(ABC):
    """Architecture doc §11's adapter boundary (`install`, `list`, `info`,
    `start`, `remove`).
    Subclasses implement the `_do_*` methods; the public methods here
    handle input validation so it's identical across every
    implementation.
    """

    #: The provider name machines.yaml uses for this implementation.
    name: str = ""

    #: True only for a simulation whose machines exist nowhere on the
    #: network: the orchestrator then skips its own TCP readiness probe
    #: (instance_manager), which could never succeed against them.
    simulated_reachability: bool = False

    async def install(self, hostname: str, image: str) -> None:
        validate_hostname(hostname)
        validate_image_name(image)
        await self._do_install(hostname, image)

    async def list(self) -> list[VM]:
        return await self._do_list()

    async def info(self, hostname: str) -> VMInfo:
        validate_hostname(hostname)
        return await self._do_info(hostname)

    async def start(self, hostname: str) -> None:
        validate_hostname(hostname)
        await self._do_start(hostname)

    async def remove(self, hostname: str) -> None:
        validate_hostname(hostname)
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
        no-op by default, so main.py can call it on every provider at
        shutdown.
        """

    async def install_idempotent(self, hostname: str, image: str) -> None:
        """Like `install()`, but safe to call again after a prior attempt
        whose outcome is unknown (architecture doc §14.4): after a
        timeout, `info()` decides whether the install happened.
        """
        try:
            await self.install(hostname, image)
        except ProviderTimeoutError as timeout_exc:
            try:
                await self.info(hostname)
            except VMNotFoundError:
                # Genuinely didn't happen -- the timeout was a real failure.
                raise timeout_exc from None
            # info() succeeded: the install evidently did happen despite
            # the timeout. Treat this call as an (idempotent) success.
            return

    async def remove_idempotent(self, hostname: str) -> None:
        """Like `remove()`, but safe to call again after a prior attempt
        whose outcome is unknown (architecture doc §14.4).
        """
        try:
            await self.remove(hostname)
        except ProviderTimeoutError as timeout_exc:
            try:
                await self.info(hostname)
            except VMNotFoundError:
                # Gone -- the remove evidently did happen despite the timeout.
                return
            # Still exists: the remove really didn't happen.
            raise timeout_exc from None


# --- one client per provider ----------------------------------------------


class UnknownProviderError(KeyError):
    """A lease or machine names a provider this orchestrator has no
    client for (not configured in machines.yaml at startup).
    """


class ProviderRegistry:
    """The orchestrator's clients, keyed by provider name (main.py builds
    it at startup from the providers machines.yaml uses).
    """

    def __init__(self, clients: Mapping[str, MachineProvider]) -> None:
        unknown = set(clients) - set(PROVIDERS)
        if unknown:
            raise ValueError(f"unknown provider name(s): {', '.join(sorted(unknown))}")
        self._clients = dict(clients)

    def get(self, provider: str) -> MachineProvider:
        try:
            return self._clients[provider]
        except KeyError:
            raise UnknownProviderError(f"no client for provider {provider!r}") from None

    def items(self) -> Iterator[tuple[str, MachineProvider]]:
        return iter(self._clients.items())

    def __contains__(self, provider: str) -> bool:
        return provider in self._clients

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
