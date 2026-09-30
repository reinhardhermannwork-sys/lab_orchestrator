"""Lease lifecycle states + legal transitions (architecture doc §6).

Pure, dependency-free: given a current `InstanceState` and an `Event`,
`next_state()` returns the next `InstanceState` or raises
`IllegalTransition`. No DB, no tux2lab, no network — this module imports
nothing outside the stdlib, so it's trivially unit-testable and safe for
every other layer to depend on without pulling anything else in.

Diagram (architecture doc §6):

    REQUESTED -> PROVISIONING -> STARTING -> WAITING_READY -> READY
    -> CONNECTED <-> DISCONNECTED_GRACE -> DESTROYING -> DESTROYED
    FAILED -> CLEANUP -> DESTROYED           (reachable from any state)
    DESTROYING reachable from any state on the 4h lifetime cap

Two judgment calls made encoding this, both worth knowing about:

1. **`reconnect` direction.** The diagram's ASCII art draws `reconnect`
   as a branch off `CONNECTED` with a line that trails off the page
   without a closed arrowhead — genuinely ambiguous on paper. §7's prose
   ("disconnected -> enters 5-minute grace period -> destroy if no
   reconnect") settles it: reconnecting during the grace period is the
   alternative to being destroyed, i.e. `DISCONNECTED_GRACE
   --RECONNECTED--> CONNECTED`, not the other way around.

2. **Scope of "from any state."** Taken literally as *every* state
   including `DESTROYING`/`FAILED`/`CLEANUP`/`DESTROYED`, "from any
   state" would produce nonsensical or redundant edges (a destroy
   already in progress re-triggering itself, or an edge out of the one
   state that must have none). This module scopes both `LIFETIME_EXPIRED`
   and `FAILED` to the *pre-destroy* states only —
   `_PRE_DESTROY_STATES` below — which is this module's own reading of
   an intentionally-terse diagram, not something spelled out explicitly
   in the architecture doc.

`DESTROYED` has no outgoing transition at all. That's not just a
modeling choice — db/models.py's max-3-global quota trigger only guards
`INSERT`, on the assumption that nothing can ever `UPDATE` a row's state
back out of `DESTROYED` to grow the active count. Any change here that
adds an edge out of `DESTROYED` needs that trigger revisited too.
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


class Event(str, Enum):
    """Signals that drive a transition — named for the thing that just
    happened (typically reported by instance_manager.py, M5), not for
    the state it leads to.
    """

    START_PROVISIONING = "START_PROVISIONING"  # tux2lab vm install kicked off
    INSTALL_COMPLETE = "INSTALL_COMPLETE"  # tux2lab vm install succeeded
    START_ISSUED = "START_ISSUED"  # tux2lab vm start called
    READY_CRITERIA_MET = "READY_CRITERIA_MET"  # VM_STATE==running AND OS_STATE==healthy AND TCP/22 reachable
    TUNNEL_OPENED = "TUNNEL_OPENED"  # Guacamole tunnel established (unused until M10/M11)
    TUNNEL_CLOSED = "TUNNEL_CLOSED"  # Guacamole tunnel closed (unused until M10/M11)
    RECONNECTED = "RECONNECTED"  # tunnel reopened during grace (unused until M10/M11)
    GRACE_EXPIRED = "GRACE_EXPIRED"  # 5 min disconnect grace elapsed, no reconnect (janitor, M6)
    LIFETIME_EXPIRED = "LIFETIME_EXPIRED"  # 4h hard cap reached (janitor, M6)
    FAILED = "FAILED"  # something went wrong
    START_CLEANUP = "START_CLEANUP"  # begin tearing down a FAILED instance
    DESTROY_COMPLETE = "DESTROY_COMPLETE"  # tux2lab vm remove succeeded / VM confirmed gone


class IllegalTransition(ValueError):
    """Raised by `next_state()` for any (state, event) pair that isn't a
    legal edge — including every event at all from `DESTROYED`.
    """


S = InstanceState
E = Event

# Non-terminal, pre-destroy states. LIFETIME_EXPIRED and FAILED are each
# legal from every one of these (see judgment call #2 above).
_PRE_DESTROY_STATES: tuple[InstanceState, ...] = (
    S.REQUESTED,
    S.PROVISIONING,
    S.STARTING,
    S.WAITING_READY,
    S.READY,
    S.CONNECTED,
    S.DISCONNECTED_GRACE,
)

_TRANSITIONS: dict[tuple[InstanceState, Event], InstanceState] = {
    (S.REQUESTED, E.START_PROVISIONING): S.PROVISIONING,
    (S.PROVISIONING, E.INSTALL_COMPLETE): S.STARTING,
    (S.STARTING, E.START_ISSUED): S.WAITING_READY,
    (S.WAITING_READY, E.READY_CRITERIA_MET): S.READY,
    (S.READY, E.TUNNEL_OPENED): S.CONNECTED,
    (S.CONNECTED, E.TUNNEL_CLOSED): S.DISCONNECTED_GRACE,
    (S.DISCONNECTED_GRACE, E.RECONNECTED): S.CONNECTED,
    (S.DISCONNECTED_GRACE, E.GRACE_EXPIRED): S.DESTROYING,
    (S.DESTROYING, E.DESTROY_COMPLETE): S.DESTROYED,
    (S.FAILED, E.START_CLEANUP): S.CLEANUP,
    (S.CLEANUP, E.DESTROY_COMPLETE): S.DESTROYED,
}

# "From any (pre-destroy) state" edges, added programmatically so there's
# one source of truth per edge *type* rather than 14 hand-copied lines.
for _s in _PRE_DESTROY_STATES:
    _TRANSITIONS[(_s, E.LIFETIME_EXPIRED)] = S.DESTROYING
    _TRANSITIONS[(_s, E.FAILED)] = S.FAILED
del _s


def next_state(current: InstanceState, event: Event) -> InstanceState:
    """Return the state `current` transitions to on `event`.

    Raises `IllegalTransition` if `(current, event)` isn't a legal edge.
    """
    try:
        return _TRANSITIONS[(current, event)]
    except KeyError:
        raise IllegalTransition(
            f"no transition from {current.value} on event {event.value}"
        ) from None
