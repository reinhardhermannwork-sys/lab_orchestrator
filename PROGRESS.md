# Lab Orchestrator — Progress Log

**Status as of this writing:** M0–M5 complete (of M0–M9). 104 tests
passing, `ruff` clean. Next up: M6 (janitor).

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

## M4 — Tux2Lab adapter

`Tux2LabClient` (abstract, matches the architecture doc §11's 5-method
interface exactly), `FakeTux2LabClient` (in-memory — what M5/M6 develop
against day to day), and `SSHTux2LabClient` (real, `asyncssh`-based).
Input validation lives once on the base class's public methods, so both
implementations enforce identical allowed-charset checks before anything
reaches the SSH boundary — not just matching method signatures.

**Judgment calls:**
- `install_idempotent`/`remove_idempotent` were added beyond the
  architecture doc's literal 5-method interface, to implement §14.4's
  "check `info`/`list` before retrying a mutating call" requirement once,
  on the base class, rather than leaving every caller to reimplement it.
- `VMInfo` deliberately excludes TCP/22 reachability — tux2lab has no way
  to know how reachable a VM is from the orchestrator container's network
  position, so that check belongs in M5's readiness-polling logic, not
  this adapter.
- `known_hosts` defaults to not being passed to `asyncssh.connect()` at
  all (fail-closed: check `~/.ssh/known_hosts`, refuse if no entry)
  rather than the more obvious-looking `known_hosts=None`, which actually
  *disables* host-key verification — a real MITM exposure this codebase
  never defaults to.

**Genuine unknowns, surfaced rather than silently resolved:** two things
about the real host wrapper can't be confirmed because it doesn't exist
yet for this codebase to inspect — its exact command syntax (assumed to
mirror tux2lab's own CLI, since that's the only documented syntax) and
`vm list`/`vm info`'s output format (assumed JSON, with "not found"
detected by a stderr-text heuristic since there's no documented distinct
signal). Both are isolated in small, clearly-marked functions in
`SSHTux2LabClient` specifically so they're an easy, obvious fix once a
real host exists to test against. Worth knowing: this means
`install_idempotent`/`remove_idempotent`'s retry-safety is fully reliable
against `FakeTux2LabClient` today, but may not reliably detect
"already gone" vs. "genuinely still failing" against a real host until
that not-found signal is confirmed.

**Worth knowing:** before writing any of `SSHTux2LabClient`, I checked
`asyncssh`'s actual public API rather than working from memory —
`connect()`'s real keyword arguments, the `known_hosts=None`
disable-checking behavior (confirmed via the maintainer's own GitHub
reply), `run()`/`SSHCompletedProcess`/`ProcessError`'s real attribute
names, and `TimeoutError`'s slightly surprising class hierarchy (it's a
distinct `asyncssh.process.TimeoutError`, not an alias for the builtin,
though it does inherit from both). Unlike tux2lab's CLI, `asyncssh` is
public and checkable, so there was no reason to guess at it the way the
wrapper's syntax had to be.

**Verified:** the full API surface was prototyped directly in the
sandbox — key generation, a real local SSH server, a real client
connection, real command execution and timeout behavior — before any of
it went into the actual module. The test suite then runs the *same*
lifecycle-contract test against both `FakeTux2LabClient` and
`SSHTux2LabClient` (the latter talking to a real local `asyncssh` server
standing in for the wrapper), so "the fake client passes the same test
suite as the real one" is demonstrated, not just asserted. Separately
verified: a malicious hostname/image never reaches the SSH call at all
(checked via the fake server's received-command log, not just that an
exception was raised), a real timeout against a real hung command raises
the right exception type, and both idempotent-retry helpers were tested
for both outcomes — recovering from an ambiguous failure and correctly
propagating a genuine one.

## M5 — Instance manager + REST API

`instance_manager.create_instance()` (fast, synchronous: validate,
quota-check via M2's DB constraints, insert) and `provision_instance()`
(the full async driver, kicked off as a tracked background task —
install → start → poll for readiness → `READY`, or a clean
`FAILED → CLEANUP → DESTROYED` on any failure, every transition going
through M3's `next_state()`). `POST`/`GET /v1/instances` wired up
end-to-end.

**Real bugs caught by actually running the tests, not just writing
them** — worth naming both, since neither was something a design review
would have caught:

1. The `202` response initially showed `state: "REQUESTED"`. Architecture
   doc §8's example is explicit that it should already show
   `"PROVISIONING"`. First test run failed on exactly this, correctly.
   Fixed by moving the `REQUESTED -> PROVISIONING` transition into
   `create_instance()` itself, synchronous, before the response is
   built — `provision_instance()` no longer repeats it.
2. The first API integration test file quietly shared the real
   `orchestrator.db` file across every test in it (the `fast_settings`
   fixture set poll-interval/timeout env vars but never overrode
   `LAB_ORCH_DB_PATH`), so instances created by one test looked "still
   active" to the next one. Surfaced as two tests failing with an
   unexpected `409` instead of `202`. Fixed by giving the fixture its
   own `tmp_path`-based DB, matching the pattern already used everywhere
   else.

**Judgment calls:**
- **Two of the three open API-design questions (§14.6, §14.7) are
  decided, not deferred further**: `POST` while a user already has an
  active instance is rejected (`409`), not treated as "return the
  existing one" — the DB constraint already makes rejection the natural
  behavior, and a v1 prototype has no stated need for the extra lookup
  the alternative would require. List/delete endpoints are out of scope
  for this milestone — not built, not silently assumed unneeded either.
- **The third (§5, DNS-suffix in the hostname) is the one real blocker**,
  per the implementation plan's own explicit instruction not to guess at
  it. `naming.py` has a real, importable interface
  (`generate_hostname(machine) -> str`) that the rest of M5 already
  calls correctly through — it just raises `NotImplementedError` in
  production until this is answered. Every other path through M5 is
  fully built, tested, and verified; only this one function's body is
  missing.
- TCP/22 reachability lives in `instance_manager.py`, not the M4
  adapter — tux2lab itself has no way to know how reachable a VM is from
  the orchestrator's own network position.

**Verified:**
- Every test in `tests/test_instance_manager.py` and
  `tests/test_api_instances.py` that needs provisioning to actually
  reach `READY` substitutes a stand-in hostname generator via
  `monkeypatch.setattr(naming, "generate_hostname", ...)` (patching the
  module attribute, not a rebound import — matters, since
  `instance_manager.py` calls it as `naming.generate_hostname(...)` at
  call time specifically so this works) and a stand-in TCP check
  (`FakeTux2LabClient`'s fixed fake IP isn't actually reachable from this
  sandbox). `test_full_workflow_reaches_ready` reproduces architecture
  doc §8's curl workflow against the real app end-to-end this way — the
  literal M5 done-when, minus the one blocked piece.
- Separately, against a real running server with the *actual*,
  still-blocked `naming.py`: `POST` returns `202`, the background task
  correctly fails at the hostname-generation step, and the row lands
  cleanly in `DESTROYED` with the real `NotImplementedError` message
  recorded as `failure_reason` — confirmed both via the API's own `GET`
  response and by reading the SQLite file directly. Also manually
  confirmed the `404` (unknown machine type, unknown instance id) and
  `400` (disabled machine) paths against a live server.
- The timing-sensitive new tests (polling loops, a short readiness
  timeout) were re-run 8x before considering them reliable, following
  the same discipline as M2's concurrency tests.

---

## M5 close-out — hostname decision and `naming.py`

The one blocker on M5 (architecture doc §5) was answered by the human:
**there is no purpose in including the username**, so hostnames are opaque
and the orchestrator neither builds nor stores a `.{username}.internal`
suffix. `naming.generate_hostname(machine)` produces
`lab-<code>-<codename>-<5 chars of lowercase Crockford base32>` using
`secrets.choice`, and takes no user input, so it can't leak a username by
construction.

**Consequences:**
- The stand-in hostname generator that M5's tests used is no longer needed
  for the end-to-end paths: `test_full_workflow_reaches_ready` and a new
  instance-manager test now run against the real generator. Only the
  TCP/22 check is still stubbed.
- The "naming still blocked fails cleanly" test and the matching
  `except NotImplementedError` branch were removed as dead code. The broad
  `except Exception` safety net still covers unexpected failures.
- Architecture doc §5/§8/§13/§14.5/§15 and the implementation plan's v1
  definition-of-done were updated to record the decision.

**Still unverified:** whether the short label resolves from a client's
network, or whether users should connect via the `ip` field. That depends
on tux2lab's DNS behavior and needs a real host to confirm.

**Verified:** `pytest` (104 passed) and `ruff` clean.

---

## Open questions still outstanding

None blocking. The open item above (short-label resolvability) needs a real
tux2lab host.

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


## M6
- Janitor cleanup loop implemented: lease expiry, disconnect grace expiry, idempotent VM cleanup, retryable DESTROYING state, and background startup wiring.
