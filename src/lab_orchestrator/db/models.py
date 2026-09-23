"""Table definitions (architecture doc §9): machine_definitions, instances.

M2 scope: the two tables exactly as specified, plus DB-level quota
guarantees (not just app-level checks):
  - partial unique index enforcing at most one "active" instance per user_id
  - an atomic check-and-reserve mechanism for the global max-3 slot

`user_id` is a plain string column (architecture doc §4) — not a UUID
type — so swapping in real identity later is a call-site change, not a
schema migration.

Not implemented yet.
"""
