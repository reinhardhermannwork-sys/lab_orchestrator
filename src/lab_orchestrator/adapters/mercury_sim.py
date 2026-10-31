"""The Mercury simulation provider (architecture doc §18).

A lease of this provider is a **machine session**: a simulated Mercury
tool (MERCSC0000 under Wine, its own plant simulator msim, screens over
VNC) stays up until the lease ends, and recipes run on it one after
another as **runs**. On the host, the CLI `mercury-sim` (mercury-lab
`docs/sim-host.md`) does the work behind the same restricted SSH wrapper
as tux2lab.

The host CLI's states in the orchestrator's terms (sim-host.md):

- `vm_state`: `created | running | exited` -- the Mercury container;
  `exited` while the lease is active means Mercury is gone.
- `os_state`: `starting` until the SECS host is online, `healthy` while it
  is, `link-lost` when the link dropped, `n/a` when not running.

Only `healthy` counts towards READY. The janitor's watchdog turns
`link-lost`, a stopped Mercury, or a missing lease into FAILED with that
reason instead of a lease that hangs.

M13 step SP1 has only `FakeMercurySimProvider`, the in-memory stand-in
the orchestrator is developed and tested against; the SSH client comes
with the host CLI (SP2) and its wrapper (SP3).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from lab_orchestrator.adapters.provider import (
    RUN_FINISHED_STATES,
    VM,
    MachineProvider,
    ProviderCommandError,
    RunInfo,
    RunRefusedError,
    VMInfo,
    VMNotFoundError,
)

# The host CLI's os_state values (sim-host.md).
OS_STARTING = "starting"
OS_HEALTHY = "healthy"
OS_LINK_LOST = "link-lost"
OS_NA = "n/a"


class MercurySimProvider(MachineProvider):
    """The mercury-sim provider: a simulated Mercury per lease, with a
    machine setup, runs, and the watchdog."""

    name = "mercury-sim"
    supports_setup = True
    supports_runs = True
    watchdog = True


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class FakeMercurySimProvider(MercurySimProvider):
    """In-memory simulation of the host CLI for local dev and tests.

    A started lease reports `starting` for `starting_polls` info() calls,
    then `healthy`. Runs advance one step per runs() call (queued ->
    running -> done) when `auto_advance` is on; tests that want to place
    each step themselves turn it off and use `set_run()`. `link_lost()`
    and `mercury_gone()` inject the two faults the watchdog handles.
    """

    def __init__(
        self,
        *,
        simulated_reachability: bool = False,
        starting_polls: int = 0,
        auto_advance: bool = True,
    ) -> None:
        self.simulated_reachability = simulated_reachability
        self.starting_polls = starting_polls
        self.auto_advance = auto_advance
        self._leases: dict[str, VMInfo] = {}
        self._starting_left: dict[str, int] = {}
        self._runs: dict[str, list[RunInfo]] = {}
        # What install() was given, per lease (tests read it).
        self.images: dict[str, str] = {}
        self.setups: dict[str, str | None] = {}
        # Test hook: if set, the *next* _do_* call raises this once.
        self.raise_once: Exception | None = None

    async def _maybe_raise_once(self) -> None:
        if self.raise_once is not None:
            exc, self.raise_once = self.raise_once, None
            raise exc

    def _lease(self, hostname: str) -> VMInfo:
        try:
            return self._leases[hostname]
        except KeyError:
            raise VMNotFoundError(hostname) from None

    # --- the host CLI's verbs --------------------------------------------

    async def _do_install(self, hostname: str, image: str, setup_json: str | None = None) -> None:
        await self._maybe_raise_once()
        if hostname in self._leases:
            raise ProviderCommandError(f"lease already exists: {hostname}", exit_status=1)
        self._leases[hostname] = VMInfo(
            hostname=hostname, vm_state="created", os_state=OS_NA, ip_address=None
        )
        self._runs[hostname] = []
        self.images[hostname] = image
        self.setups[hostname] = setup_json

    async def _do_list(self) -> list[VM]:
        await self._maybe_raise_once()
        return [VM(hostname=v.hostname, vm_state=v.vm_state) for v in self._leases.values()]

    async def _do_info(self, hostname: str) -> VMInfo:
        await self._maybe_raise_once()
        info = self._lease(hostname)
        if info.os_state == OS_STARTING:
            if self._starting_left.get(hostname, 0) > 0:
                self._starting_left[hostname] -= 1
            else:
                info = replace(info, os_state=OS_HEALTHY)
                self._leases[hostname] = info
        return info

    async def _do_start(self, hostname: str) -> None:
        await self._maybe_raise_once()
        info = self._lease(hostname)
        # The container's name on the Docker network is the lease name.
        self._leases[hostname] = replace(
            info, vm_state="running", os_state=OS_STARTING, ip_address=hostname
        )
        self._starting_left[hostname] = self.starting_polls

    async def _do_remove(self, hostname: str) -> None:
        await self._maybe_raise_once()
        self._lease(hostname)
        del self._leases[hostname]
        # The lease folder (and its runs) stays on the host, as sim-host.md
        # says; only the containers go.

    async def _do_run(self, hostname: str, run_id: str, recipe: str) -> RunInfo:
        await self._maybe_raise_once()
        info = self._lease(hostname)
        if info.vm_state != "running" or info.os_state != OS_HEALTHY:
            raise RunRefusedError(f"lease {hostname} is not online", exit_status=1)
        runs = self._runs[hostname]
        if any(r.state not in RUN_FINISHED_STATES for r in runs):
            raise RunRefusedError(f"lease {hostname} has a run going", exit_status=1)
        run = RunInfo(run_id=run_id, recipe=recipe, state="queued")
        runs.append(run)
        return run

    async def _do_runs(self, hostname: str) -> list[RunInfo]:
        await self._maybe_raise_once()
        if hostname not in self._runs:
            raise VMNotFoundError(hostname)
        if self.auto_advance and hostname in self._leases:
            self._advance(hostname)
        return list(self._runs[hostname])

    def _advance(self, hostname: str) -> None:
        runs = self._runs[hostname]
        for i, run in enumerate(runs):
            if run.state == "queued":
                runs[i] = replace(run, state="running", started_at=_now_iso())
            elif run.state == "running":
                runs[i] = replace(
                    run,
                    state="done",
                    ended_at=_now_iso(),
                    result="PROCESS_DONE",
                    blobs=[f"runs/{run.run_id}/blob-0001.bin"],
                )

    # --- test hooks -------------------------------------------------------

    def link_lost(self, hostname: str) -> None:
        """The SECS host lost its link to Mercury."""
        self._leases[hostname] = replace(self._lease(hostname), os_state=OS_LINK_LOST)

    def mercury_gone(self, hostname: str) -> None:
        """The Mercury container stopped on its own."""
        self._leases[hostname] = replace(
            self._lease(hostname), vm_state="exited", os_state=OS_NA, ip_address=None
        )

    def set_run(self, hostname: str, run_id: str, **changes: object) -> None:
        """Change one run's fields as the host would report them."""
        runs = self._runs[hostname]
        for i, run in enumerate(runs):
            if run.run_id == run_id:
                runs[i] = replace(run, **changes)  # type: ignore[arg-type]
                return
        raise KeyError(run_id)
