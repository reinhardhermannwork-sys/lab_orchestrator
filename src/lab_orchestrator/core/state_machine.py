"""Lease lifecycle states + legal transitions (architecture doc §6).

`InstanceState` — the fixed vocabulary of states — is defined here early
because M2's `instances.state` CHECK constraint needs it. The transition
function itself is still M3's job:

  Given a current state and an event, return the next state (or raise on
  an illegal transition). Pure, dependency-free — no DB, no tux2lab, no
  network — so it's trivially unit-testable.

States:
  REQUESTED -> PROVISIONING -> STARTING -> WAITING_READY -> READY
  -> CONNECTED -> DISCONNECTED_GRACE -> DESTROYING -> DESTROYED
  FAILED -> CLEANUP -> DESTROYED   (reachable from any state)

DESTROYED has no outgoing transition — every layer above leans on that
being permanently true, most concretely db/models.py's max-3-global
quota trigger, which only guards INSERT because nothing can UPDATE a row
back out of DESTROYED to grow the active count.

Not implemented yet: the transition function itself (M3).
"""

from __future__ import annotations

from enum import Enum


class InstanceState(str, Enum):
    REQUESTED = "REQUESTED"
    PROVISIONING = "PROVISIONING"
    STARTING = "STARTING"
    WAITING_READY = "WAITING_READY"
    READY = "READY"
    CONNECTED = "CONNECTED"
    DISCONNECTED_GRACE = "DISCONNECTED_GRACE"
    DESTROYING = "DESTROYING"
    DESTROYED = "DESTROYED"
    FAILED = "FAILED"
    CLEANUP = "CLEANUP"
