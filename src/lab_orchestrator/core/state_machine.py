"""Lease lifecycle states + legal transitions (architecture doc §6).

M3 scope: a pure, dependency-free module — given a current state and an
event, return the next state or raise on an illegal transition. No DB,
no tux2lab, no network, so it's trivially unit-testable.

States:
  REQUESTED -> PROVISIONING -> STARTING -> WAITING_READY -> READY
  -> CONNECTED -> DISCONNECTED_GRACE -> DESTROYING -> DESTROYED
  FAILED -> CLEANUP -> DESTROYED   (reachable from any state)

Not implemented yet.
"""
