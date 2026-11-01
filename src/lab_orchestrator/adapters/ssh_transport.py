"""SSH to the host-side restricted wrapper (architecture doc §11), shared
by the providers that reach a host CLI this way: `SSHTux2LabClient`
(tux2lab, M4/M10) and `SSHMercurySimClient` (mercury-sim, M13).

One lazily opened, reused connection per client. `exec()` returns the
exit status and both output streams; reading them is each provider's own
business (tux2lab prints coloured text with errors on stdout, mercury-sim
prints JSON).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lab_orchestrator.adapters.provider import ProviderConnectionError, ProviderTimeoutError

if TYPE_CHECKING:
    import asyncssh

# Sentinel distinguishing "don't pass known_hosts at all" (asyncssh's own
# default: check against ~/.ssh/known_hosts, fail closed if there's no
# entry) from an explicit `known_hosts=None` (which *disables* host-key
# checking entirely -- a real security downgrade, never the default here).
UNSET = object()


def known_hosts_option(path: Path | None) -> object:
    """A settings path as the `known_hosts` argument: UNSET when not
    configured, which keeps asyncssh's checking -- never None."""
    return UNSET if path is None else str(path)


@dataclass
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str


class SSHTransport:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        client_keys: list[str],
        known_hosts: object = UNSET,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._client_keys = client_keys
        self._known_hosts = known_hosts
        self._conn: asyncssh.SSHClientConnection | None = None

    async def _connection(self) -> asyncssh.SSHClientConnection:
        import asyncssh

        if self._conn is None or self._conn.is_closed():
            connect_kwargs: dict = {
                "host": self._host,
                "port": self._port,
                "username": self._username,
                "client_keys": self._client_keys,
            }
            if self._known_hosts is not UNSET:
                connect_kwargs["known_hosts"] = self._known_hosts
            try:
                self._conn = await asyncssh.connect(**connect_kwargs)
            except (OSError, asyncssh.Error) as exc:
                raise ProviderConnectionError(str(exc)) from exc
        return self._conn

    async def exec(self, command: str, *, timeout: float) -> CommandResult:
        """Run `command` (already validated and quoted by the caller).
        Raises ProviderTimeoutError (outcome unknown, §14.4) or
        ProviderConnectionError; any exit status is returned as is.
        """
        import asyncssh

        conn = await self._connection()
        try:
            result = await conn.run(command, check=False, timeout=timeout)
        except asyncssh.TimeoutError as exc:
            raise ProviderTimeoutError(command) from exc
        except asyncssh.Error as exc:
            raise ProviderConnectionError(str(exc)) from exc
        return CommandResult(
            exit_status=result.exit_status if result.exit_status is not None else -1,
            stdout=str(result.stdout or ""),
            stderr=str(result.stderr or ""),
        )

    async def close(self) -> None:
        if self._conn is not None and not self._conn.is_closed():
            self._conn.close()
            await self._conn.wait_closed()
