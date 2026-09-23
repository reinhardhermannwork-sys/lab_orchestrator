"""Tests for the pure state machine (M3 done-when: every transition in
the diagram has a test, and at least one illegal transition is proven
to raise).
"""

import pytest

from lab_orchestrator.core.state_machine import (
    Event,
    IllegalTransition,
    InstanceState,
    next_state,
)

S = InstanceState
E = Event

# Every legal (state, event) -> state edge in the diagram (architecture
# doc §6), spelled out explicitly here rather than derived from the
# module's own table — a test that just re-imports the thing under test
# proves nothing.
LEGAL_TRANSITIONS = [
    (S.REQUESTED, E.START_PROVISIONING, S.PROVISIONING),
    (S.PROVISIONING, E.INSTALL_COMPLETE, S.STARTING),
    (S.STARTING, E.START_ISSUED, S.WAITING_READY),
    (S.WAITING_READY, E.READY_CRITERIA_MET, S.READY),
    (S.READY, E.TUNNEL_OPENED, S.CONNECTED),
    (S.CONNECTED, E.TUNNEL_CLOSED, S.DISCONNECTED_GRACE),
    (S.DISCONNECTED_GRACE, E.RECONNECTED, S.CONNECTED),
    (S.DISCONNECTED_GRACE, E.GRACE_EXPIRED, S.DESTROYING),
    (S.DESTROYING, E.DESTROY_COMPLETE, S.DESTROYED),
    (S.FAILED, E.START_CLEANUP, S.CLEANUP),
    (S.CLEANUP, E.DESTROY_COMPLETE, S.DESTROYED),
]

# "From any (pre-destroy) state" edges — the 4h hard cap and failure
# branch (architecture doc §6, scoped per state_machine.py's own
# documented reading of "any state").
PRE_DESTROY_STATES = [
    S.REQUESTED,
    S.PROVISIONING,
    S.STARTING,
    S.WAITING_READY,
    S.READY,
    S.CONNECTED,
    S.DISCONNECTED_GRACE,
]
for _s in PRE_DESTROY_STATES:
    LEGAL_TRANSITIONS.append((_s, E.LIFETIME_EXPIRED, S.DESTROYING))
    LEGAL_TRANSITIONS.append((_s, E.FAILED, S.FAILED))


@pytest.mark.parametrize("current,event,expected", LEGAL_TRANSITIONS)
def test_legal_transition(current, event, expected):
    assert next_state(current, event) == expected


def test_all_non_legal_state_event_pairs_raise():
    """Exhaustive, not just spot-checked: for every (state, event)
    combination *not* in LEGAL_TRANSITIONS, next_state must raise. This
    proves the table has no accidental gaps *and* no accidental extra
    edges — stronger than testing one hand-picked illegal example alone.
    """
    legal_pairs = {(t[0], t[1]) for t in LEGAL_TRANSITIONS}
    checked = 0
    for state in InstanceState:
        for event in Event:
            if (state, event) in legal_pairs:
                continue
            checked += 1
            with pytest.raises(IllegalTransition):
                next_state(state, event)
    # Sanity check on the sweep itself: make sure it actually covered a
    # substantial number of illegal pairs, not zero due to a bug above.
    assert checked > 50


def test_ready_to_provisioning_is_illegal():
    """The specific example named in the implementation plan's M3
    done-when criterion ("READY -> PROVISIONING")."""
    with pytest.raises(IllegalTransition):
        next_state(S.READY, E.START_PROVISIONING)


def test_destroyed_has_no_outgoing_transitions():
    """db/models.py's max-3-global quota trigger depends on this being
    permanently true — see that module's docstring.
    """
    for event in Event:
        with pytest.raises(IllegalTransition):
            next_state(S.DESTROYED, event)


def test_illegal_transition_message_names_state_and_event():
    with pytest.raises(IllegalTransition, match="READY") as exc_info:
        next_state(S.READY, E.START_PROVISIONING)
    assert "START_PROVISIONING" in str(exc_info.value)


def test_reconnect_returns_to_connected_not_the_other_way():
    """Pins down the ambiguous diagram arrow, per §7's prose ('destroy
    if no reconnect') — reconnecting during grace goes back to CONNECTED.
    """
    assert next_state(S.DISCONNECTED_GRACE, E.RECONNECTED) == S.CONNECTED
    with pytest.raises(IllegalTransition):
        next_state(S.CONNECTED, E.RECONNECTED)


def test_every_instance_state_is_reachable_as_a_target():
    """Sanity check on the table: every state should be the destination
    of at least one legal transition, except REQUESTED, which is the
    fixed entry point nothing transitions into.
    """
    targets = {t[2] for t in LEGAL_TRANSITIONS}
    unreachable = set(InstanceState) - targets - {S.REQUESTED}
    assert not unreachable, f"states never reached by any transition: {unreachable}"


def test_lifetime_expired_not_legal_from_destroying_failed_cleanup_destroyed():
    """Pins down judgment call #2 in the module docstring: 'any state'
    is scoped to pre-destroy states, not literally every state.
    """
    for state in (S.DESTROYING, S.FAILED, S.CLEANUP, S.DESTROYED):
        with pytest.raises(IllegalTransition):
            next_state(state, E.LIFETIME_EXPIRED)
