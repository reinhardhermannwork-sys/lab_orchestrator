# Lab Orchestrator — Progress Log

**Status as of this writing:** M0–M3 complete (of M0–M9). 61 tests passing,
`ruff` clean, four commits. Next up: M4 (Tux2Lab adapter).

This is a narrative log, not reference docs — see `README.md` for setup
instructions and current project state. This file exists to answer "what
happened and what's worth knowing about it," including the judgment calls
made along the way and how each milestone was actually verified, not just
what got committed.

---

## M0 — Scaffolding

Repo skeleton per the implementation plan's suggested layout, `pyproject.toml`
with the suggested stack (FastAPI, SQLAlchemy Core, `asyncssh`, `python-ulid`),
every non-`main.py` module stubbed with a docstring naming which milestone
fills it in. `config/machines.yaml` populated from the architecture doc's own
example. `GET /healthz` wired up.

**Verified:** `pip install -e ".[dev]"` installs clean; `pytest` passes;
real `uvicorn lab_orchestrator.main:app` boots and `curl /healthz` returns
`200 {"status":"ok"}` — checked against the actual process, not just
`TestClient`.

Git repo initialized here; every milestone since is its own commit.

## M1 — Config & machine definitions

`Settings` (`pydantic-settings`, `LAB_ORCH_*` env vars) and the
`machines.yaml` loader/validator, wired into `main.py`'s startup `lifespan`
so invalid config kills the process rather than just failing a function call
somewhere.

**Judgment calls:**
- SSH connection details for the tux2lab host wrapper are deliberately
  *not* in `Settings` yet — that's an M4 design decision (asyncssh's exact
  connection shape isn't settled), not something to guess at now.
- `protocol` is constrained to `Literal["ssh"]` for v1. The architecture
  doc lists it as future-RDP-ready but SSH-only today; a config with
  `protocol: rdp` now fails validation instead of silently loading
  something nothing else handles.
- `code`/`codename` are validated against `^[a-z0-9]+$` — a config-time
  check, distinct from the runtime SSH-injection-safety validation M4
  will need for values that actually cross the SSH boundary.

**Worth knowing:** I made a real bug while building this and caught it
before it shipped. `pydantic-settings` derives env var names from the
*field* name, so `machines_config_path` → `LAB_ORCH_MACHINES_CONFIG_PATH`,
not the shorter `LAB_ORCH_MACHINES_CONFIG` I used in my first draft of a
test. The test passed anyway — it silently fell back to the default config
file, a false green. Caught by checking real behavior (a real `uvicorn`
process against a genuinely broken config file, watching it actually fail)
rather than trusting the test in isolation. Documented in `README.md`'s
Configuration section so the next person adding a field doesn't repeat it.

**Verified:** real `uvicorn` boot with a broken `machines.yaml` (via env
var) prints `Application startup failed. Exiting.` with a clear traceback;
valid config still boots and serves `/healthz`.

## M2 — Data layer

`machine_definitions` + `instances` tables (SQLAlchemy Core), synced from
`machines.yaml` on every boot (upsert, never delete). Two DB-level quota
guarantees, not app-level `if` checks — this was the milestone the
implementation plan explicitly called out as not-done-until-tested:

- **One active instance per user** — a partial unique index on
  `instances.user_id` covering only `state != 'DESTROYED'` rows.
- **Max 3 active instances globally** — a `BEFORE INSERT` trigger
  (SQLAlchemy Core has no native trigger construct, so this is raw DDL)
  that counts non-`DESTROYED` rows and aborts past 3.

**Judgment calls:**
- `machine_definitions.id` intentionally holds the `machine_type` string
  key (e.g. `"machine_1"`), not a surrogate integer — the architecture
  doc's column list didn't name a separate `machine_type` column, and this
  keeps one string used consistently through config, API, and DB.
- Sync SQLAlchemy engine + stdlib `sqlite3`, **not** `aiosqlite`. The
  quota trigger leans on SQLite's ordinary single-writer locking, simplest
  to reason about synchronously. M5 will wrap calls from async routes in a
  thread-pool executor rather than switch drivers.
- "Active" = `state != 'DESTROYED'`. Only safe because `DESTROYED` has no
  outgoing transition (confirmed structurally in M3) — nothing can
  `UPDATE` a row back out of it to grow the active count, so the trigger
  only needs to guard `INSERT`.

**Worth knowing:** before writing any test assertions, I empirically
checked what exception type each constraint actually raises (FK
violation, CHECK violation, partial-unique violation, trigger abort) —
all four turned out to be `sqlalchemy.exc.IntegrityError`, but I verified
that rather than assumed it, given the M1 lesson above.

**Verified:**
- The concurrency tests (the actual done-when criterion: "a test that
  fires two concurrent creates for the same user proves only one
  succeeds") use real `threading.Thread` + a `Barrier`, not sequential
  calls dressed up as concurrent ones. Run 15x in a row before committing
  — no flakiness.
- Booted a real `uvicorn` process against a real DB file and inspected it
  directly with Python's `sqlite3` module (not through the app) —
  confirmed both tables, the partial index, and the trigger exist on disk
  exactly as designed.

## M3 — State machine

`Event` enum (12 signals) + `next_state(state, event) -> state` in
`core/state_machine.py`. Pure, stdlib-only — no DB, no network, nothing
else in the codebase for it to depend on.

**Judgment calls** (the architecture doc's diagram is terse enough that
both of these were genuinely ambiguous on the page, not just
under-specified):
- **`reconnect` direction.** The ASCII diagram draws it as a line trailing
  off without a closed arrowhead. §7's prose ("destroy if no reconnect")
  settles it: reconnecting during the grace period returns to
  `CONNECTED` — it's the alternative to being destroyed, not a route
  deeper into teardown.
- **Scope of "from any state."** For the 4h lifetime cap and the failure
  branch, read as *any pre-destroy state* rather than literally every
  state — the literal reading would produce nonsense (`DESTROYING`
  re-triggering itself, or an edge out of `DESTROYED`, which must have
  none).

**Verified:** every one of the 25 legal transitions is tested explicitly
(spelled out by hand in the test file, not derived from the module's own
table — re-importing the thing under test as its own proof is worthless).
Beyond that, an exhaustive sweep checks all 107 non-legal `(state, event)`
pairs out of the full 132-pair space and confirms every one raises — a
stronger guarantee than the plan's one named example
(`READY → PROVISIONING`, also tested directly). Separately confirmed
`DESTROYED` really has zero outgoing transitions, since M2's quota trigger
silently depends on that.

---

## Open questions still outstanding

Carried forward from the architecture doc, not yet resolved by anyone:

1. **DNS suffix in the VM hostname** (architecture doc §5, §14.5) — does
   `lab-m01-aurora-7k4m2.hermann.internal` legitimately reintroduce the
   username via tux2lab's DNS zoning, or does that conflict with the
   "opaque, no-username" hostname goal? **This blocks `naming.py`**, which
   is needed by M5 — worth settling before M5 starts, even though M4
   itself doesn't need it.
2. **`POST` on an existing active instance** (§14.6) — reject outright, or
   return the existing instance? Needed for M5's API design.
3. **List/delete endpoints** (§14.7, §8) — is `GET /v1/instances` (list)
   or `DELETE /v1/instances/{id}` in scope for v1? Also needed for M5.

None of these block M4 (the Tux2Lab adapter), which is next.

## How verification has worked so far

Worth naming as a pattern, since it's been deliberate every milestone: **a
green test suite is treated as necessary, not sufficient.** Every
milestone so far has also been checked against something more real than
the test doubles — a real `uvicorn` process for M0/M1/M2, a real on-disk
SQLite file inspected outside the app for M2, an empirical check of actual
exception types before writing assertions about them, and an exhaustive
rather than spot-check sweep for M3's transition table. Two real bugs
were caught this way (the `pydantic-settings` env var name in M1) that a
narrower "does the test pass" check would have missed.
