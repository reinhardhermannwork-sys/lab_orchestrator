"""Async background cleanup loop (architecture doc §7).

M6 scope: every 10-15s, find instances where `now >= expires_at` or
`disconnect_since` is more than 5 minutes old, transition them through
DESTROYING -> DESTROYED, and call Tux2LabClient.remove.

Not implemented yet.
"""
