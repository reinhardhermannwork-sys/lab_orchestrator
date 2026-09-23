"""SSH/subprocess adapter to the tux2lab CLI (architecture doc §11).

M4 scope: Tux2LabClient with install / list / info / start / remove,
each invoking the CLI over the host-side SSH wrapper (asyncssh).

    class Tux2LabClient:
        async def install(self, hostname: str, image: str) -> None: ...
        async def list(self) -> list[VM]: ...
        async def info(self, hostname: str) -> VMInfo: ...
        async def start(self, hostname: str) -> None: ...
        async def remove(self, hostname: str) -> None: ...

Also required in M4:
  - a fake/mock implementation of the same interface, for local dev and
    tests without a real KVM host (unblocks M5/M6 in parallel)
  - strict allowed-charset validation on every value interpolated into a
    CLI invocation before it crosses the SSH boundary (architecture doc
    §14.3 — injection-safety boundary)
  - check current state via info/list before retrying a mutating call
    after a timeout/ambiguous failure (architecture doc §14.4 —
    avoids double-provisioning)

Not implemented yet.
"""
