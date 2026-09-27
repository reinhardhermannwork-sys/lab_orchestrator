"""Hostname generation (architecture doc §5).

Short form (used as the tux2lab VM identifier, the `-H` argument to
every `Tux2LabClient` call, and `instances.vm_hostname`):

    lab-<machine-code>-<codename>-<instance-suffix>
    e.g. lab-m01-aurora-7k4m2

`instance-suffix` is a short random/Crockford-base32-style identifier so
recycled or simultaneous instances never collide. It's intentionally
**opaque** — no username in the VM's own hostname component.

**BLOCKED on an explicit answer, not implemented.** Architecture doc §5
flags one example in the source discussion as using the full form
`lab-m01-aurora-7k4m2.hermann.internal` — the username reappearing in a
DNS suffix, even though the stated goal was no username in the hostname
at all. That might be entirely fine if `.{username}.internal` is simply
how tux2lab's own per-user DNS zoning works (it owns DNS/DHCP per
architecture doc §11) — but it wasn't reconciled with the "no username"
preference, and the implementation plan (§M5) is explicit: get the
answer on record before writing this module, don't bake in a guess.

`generate_hostname` exists as a real, importable function — so
core/instance_manager.py (M5) can be built, wired, and tested today with
a substitute hostname generator — but raises until the question above is
answered. See core/instance_manager.py for how it's called.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lab_orchestrator.core.config import MachineDefinition


def generate_hostname(machine: MachineDefinition) -> str:
    """Generate a VM hostname for a new instance of `machine`.

    Raises NotImplementedError until architecture doc §5's open question
    is answered — see the module docstring.
    """
    raise NotImplementedError(
        "naming.py is blocked on architecture doc §5's DNS-suffix question "
        "— see this module's docstring. Not something to guess at."
    )
