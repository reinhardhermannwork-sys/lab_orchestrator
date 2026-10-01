# Lab Orchestrator — Progress Log

**Status as of this writing:** M0–M8 complete (of M0–M12; M8 verified on
the test VM); M9 built, its traefik/authentik done-when still open. 140 tests passing, `ruff` clean. Next:
M9, the web frontend (React + Tailwind via Vite, thin TypeScript Node
server). Current order: M8
container, M9 web frontend, M10 host wrapper + real tux2lab, M11/M12
Guacamole — see "Milestone reorder" below; older sections keep the numbers
they had when written.

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

## M6 — Janitor

`core/janitor.py`, started from the FastAPI lifespan. Every
`janitor_poll_interval_seconds` (default 15s) it destroys instances whose
lease expired or whose disconnect grace (`disconnect_grace_seconds`,
default 300s) elapsed.

- Every write goes through `next_state()` (LIFETIME_EXPIRED /
  GRACE_EXPIRED -> DESTROYING, DESTROY_COMPLETE -> DESTROYED) and is a
  compare-and-set on (id, observed state), so a row changed by
  provisioning after the janitor's SELECT is left alone.
- The claim uses `UPDATE ... RETURNING`; cleanup acts on the fresh row,
  so a `vm_hostname` written after the SELECT is still removed.
- VM already gone counts as success; any other tux2lab failure leaves the
  row in DESTROYING and the next pass retries it.
- Candidates are processed independently: an unexpected error on one row
  is logged and doesn't abort the pass.
- The set of lease-expirable states is derived from the state machine, not
  copied.

**Verified:** `ruff check` clean. Also against a
real `uvicorn` process (fake tux2lab, lease 0.002h, janitor interval 2s):
a `POST`ed instance reached `DESTROYED` within one janitor cycle of expiry.

**Found during that live check, then fixed (M5 behavior change):**
`provision_instance` wrote state by id only, from its own in-memory copy.
When the janitor destroyed an instance still polling for readiness, the
provisioning task's next `info()` raised `VMNotFoundError` and `_fail()`
drove the already-DESTROYED row through FAILED -> CLEANUP -> DESTROYED
(overwriting `failure_reason`/`destroyed_at`, and briefly making a
DESTROYED row active again, which the max-3 INSERT trigger assumes never
happens). If the lease expired before install finished, the janitor saw no
`vm_hostname` and the VM was orphaned.

Now every provisioning UPDATE is a compare-and-set on the state the task
last wrote. If the row moved on, provisioning stops touching it and only
removes the VM it created (the janitor may not know the hostname).
Regression tests cover both paths and fail without the fix; the live
`uvicorn` run now ends with the janitor's DESTROYED intact and
`failure_reason` NULL. `pytest`: 116 passed (5 consecutive runs).

## M7 — Startup reconciliation

`core/reconcile.py`, run in the lifespan after DB init and before the
janitor starts (so nothing else is writing yet). Three decisions, made by
the human before implementation:

- **Leases stuck mid-transition** (`REQUESTED`/`PROVISIONING`/
  `STARTING`/`WAITING_READY`, plus `FAILED`) are failed and cleaned up,
  not just logged: moved to `CLEANUP` via `next_state()` with
  `failure_reason = "orchestrator restarted while instance was <STATE>"`.
  Log-only would have left the user locked out until lease expiry, and
  `FAILED`/`CLEANUP` leases holding a quota slot *forever* — the janitor
  never selected those states.
- **VMs no active lease claims** are logged only, never removed: the host
  may run VMs the orchestrator doesn't own.
- **Provisioning records `vm_hostname` before `install`** (an M5 behavior
  change), not together with `INSTALL_COMPLETE`. Before this, a crash
  mid-install left a VM the DB couldn't name — it would only have shown up
  as an unknown VM. The API is unchanged (`GET` still shows `hostname`
  only once `READY`).

**Janitor change:** it now finishes `CLEANUP` leases (remove VM →
`DESTROY_COMPLETE`) the same way it retries `DESTROYING`, so reconciliation
only touches the DB and removal gets the janitor's existing retry-on-failure.
Reconciliation's DB step runs even when tux2lab is unreachable; the VM
comparison is then skipped with an ERROR log, and startup continues.

**Judgment call, not asked:** an active lease (`READY`/`CONNECTED`/
`DISCONNECTED_GRACE`) whose VM is missing is logged but left as-is — the
plan only asks to surface it, and the janitor destroys it at lease expiry.

**Worth knowing:**
- A graceful shutdown mid-provisioning cancels the provisioning task
  (`main.py`'s lifespan), so it also leaves a stuck lease. Reconciliation
  covers that the same way as a crash.
- The janitor can now finish a `CLEANUP` lease while a live provisioning
  task is still inside its own `_fail()` cleanup. Benign: both removes
  are idempotent, the compare-and-set lets exactly one record
  `DESTROYED`, and the provisioning task stops as `_Superseded`. One
  wrinkle: if that `_fail()` was handling an *unexpected* exception, the
  supersede means the exception is no longer re-raised — it survives only
  as `failure_reason` (and as `__context__`), not as a loud error.
- The app configures no logging, so under `uvicorn` only WARNING and
  above from `lab_orchestrator.*` reach stderr (Python's last-resort
  handler). Reconciliation logs its drift at WARNING for that reason; the
  janitor's INFO lines are invisible in a live run today.

**Verified:** `pytest` 135 passed (5 consecutive runs), `ruff` clean. The
done-when test (`test_killed_mid_install_then_restart_leaves_no_stuck_or_ghost_instance`)
cancels provisioning after tux2lab created the VM but before
`INSTALL_COMPLETE`, then reconciles and runs the janitor: VM removed, lease
`DESTROYED`, same user can lease again. Also against a real `uvicorn`
process: `POST`, wait for `WAITING_READY`, `kill -9`, restart. Startup
printed `reconcile: instance … (user hermann, VM lab-m01-aurora-…) was
WAITING_READY when the orchestrator stopped; moved to CLEANUP…`, the lease
reached `DESTROYED` within one janitor cycle with that `failure_reason`,
and a new `POST` for the same user returned `202`.

## Test-deploy planning (docs only)

With M0–M7 done, the next goal is a test deploy on the real setup: a VPS
(`planetsexpress.dedyn.io`) running traefik, authentik, Guacamole, a web
frontend, and the orchestrator as containers, with the tux2lab CLI and KVM on
the host. The target workflow is login → request a machine in the frontend →
Guacamole session to that machine's VM. `ARCHITECTURE.md` (§1, §3, §4, §11,
§13–§17) and `IMPLEMENTATION_PLAN.md` were updated; no code changed beyond
milestone numbers in comments.

**Milestones renumbered** to follow execution order: M8 container packaging
(stage 1, fake tux2lab), M9 host wrapper + real tux2lab (completes the backend
core), M10/M11 Guacamole (were M8/M9), M12 web frontend (new; part of this
repo, its own container).

**Decisions (by the human):**
- One prefabricated golden image per machine type, containing that machine's
  tool-controller software. tux2lab will be extended separately to install a
  named image; until then the host wrapper maps image → `-d/-v`.
- The host wrapper is built in this repo (`deploy/host/`).
- The orchestrator runs on a Docker bridge network shared with the frontend
  and Guacamole; its API is never exposed outside that network.
- Staged deploy: fake client in the container first, then real tux2lab.

**Worth knowing — M4's real-host assumptions were wrong.** Reading the
tux2lab source (`github.com/Muthukumar-Subramaniam/tux2lab`) showed: `vm
install` takes `-d/-v`, not an image; `vm list`/`vm info` print colored
text, not JSON; `vm remove` prompts without `-f`; VMs are named by FQDN
(`<name>.<user>.internal`). The last one means M7's reconciliation would
report every real VM as unknown until M9 normalizes FQDNs in the adapter.
All of it is contained in `SSHTux2LabClient`'s parsers plus the wrapper —
the M4 design of isolating those guesses paid off. A closer read of the CLI
source added four more M9 items (architecture doc §11): errors are printed
to stdout, so today's stderr-based not-found check would never match; ANSI
colors are always on; a prompt without a terminal loops until timeout; and
`vm install` already starts the VM, while cloning may outlast the 30s
command timeout. Also found: libvirt's
NAT rules reject new connections from Docker into `labbr0`, so the
readiness check (and later guacd) needs a host firewall rule.

## M8 — Container packaging (implemented; VPS verification pending)

`Dockerfile` (python:3.12-slim, non-root uid 10001, non-editable install,
one uvicorn worker, Python-based `HEALTHCHECK`), `.dockerignore`,
`compose.yaml` (shared external network, `/data` volume, read-only config
and secrets mounts, `host.docker.internal:host-gateway`, no published port),
and `deploy/.env.example`.

Two code changes:
- **Logging to stdout.** `main.configure_logging()` attaches one stdout
  handler to the `lab_orchestrator` logger at `LAB_ORCH_LOG_LEVEL`. Only the
  package logger is touched, not root, and it keeps propagating so pytest's
  `caplog` still works. This closes the gap noted in M7: INFO lines from the
  janitor and reconciliation were invisible under uvicorn.
- **Explicit backend.** `LAB_ORCH_TUX2LAB_BACKEND` = `auto` (unchanged local
  behavior) | `fake` | `ssh`. With `ssh` and missing SSH settings, startup
  fails instead of silently running against the fake client.

**Judgment calls:**
- One `deploy/.env` drives both compose interpolation (network name, host
  paths) and the container's `LAB_ORCH_*` settings. The in-container paths
  (DB, config, key, known_hosts) are fixed in `compose.yaml` so they can't
  drift from the mounts.
- The container runs as a fixed uid (10001) so host-side secret files can
  be `chown`ed to it; documented in `deploy/.env.example`.

**Worth knowing:**
- The fake client's placeholder IP (10.28.28.100) is inside the real
  `labbr0` range. On the VPS a fake lease could in principle reach READY if
  a real VM holds that address; the M8 done-when is worded not to depend on
  either outcome.
- `instance_manager` logs nothing about provisioning transitions or
  failures, so a failed provisioning is visible only through `GET`/the DB,
  not in `docker logs`. Implementation plan §3 asks for structured
  transition logs; not done yet — a small follow-up worth doing before M9.

**Verified (locally, no Docker on the dev VM):** 140 tests pass, `ruff`
clean. Simulated the image: copied only what the Dockerfile copies,
installed non-editably into a fresh virtualenv, deleted `src/`, and booted
uvicorn with the image's environment and `LAB_ORCH_TUX2LAB_BACKEND=fake`.
The healthcheck command exits 0; `POST`/`GET` work; reconciliation and
janitor INFO lines appear on stdout; SQLite creates `-wal`/`-shm` next to
the DB (so the whole `/data` directory must be the volume); `backend=ssh`
without settings aborts startup with a clear error. **Not yet verified:**
`docker build`, the compose file itself, and the whole M8 done-when list —
all need the VPS.

## Milestone reorder — web frontend next

The human moved the web frontend ahead of the real-host work, so the
user-facing flow can be built and tried against the fake tux2lab backend
first. M8 (container) keeps its number since it's already implemented and
committed under it. New order and old numbers: **M9 web frontend** (was M12),
**M10 host wrapper + real tux2lab** (was M9), **M11 Guacamole JSON-auth**
(was M10), **M12 tunnel-close listener** (was M11). Docs and three code
comments were renumbered; the sections above keep their original numbers.

What M9 now has to carry, since it comes before Guacamole and the real
host: its own server side (the browser can't reach the orchestrator API),
identity only from the authentik header, a few orchestrator API additions
(machine list, "my current instance", maybe early destroy — §14.7), and a
placeholder connection view that M11 replaces with the Guacamole session.

## M9 part 1 — orchestrator API for the web frontend

The three endpoints decided for M9, plus two response fields:

- `GET /v1/machines` — enabled machine types from the loaded registry
  (`api/routes_machines.py`).
- `GET /v1/instances?user=` — that user's active leases, newest first.
  `user` is required: there's no unfiltered listing.
- `DELETE /v1/instances/{id}?user=` — early release, `202`. New state-machine
  event `USER_RELEASED` with exactly `LIFETIME_EXPIRED`'s scope (every
  pre-destroy state → `DESTROYING`), so the janitor removes the VM with no
  new cleanup code. Owner-checked: another user's id answers `404`, the same
  body as an unknown id. Idempotent for leases already in teardown.
- Instance responses now include `expires_at` (tagged UTC, so browsers
  don't read it as local time) and `failure_reason`, for the status screen.

**Judgment calls:**
- `release_instance()` writes through the janitor's compare-and-set helper
  (`janitor.transition_if`) and re-reads on a lost race (up to 5 tries, then
  it raises — a row changing that often would be a bug, not contention).
- A release while provisioning is still running needs no special case: the
  provisioning task's next compare-and-set misses, it stops as
  `_Superseded` and removes the VM it created (M6 behavior).

**Verified:** 157 tests pass (3 runs), `ruff` clean. New tests cover the
`USER_RELEASED` scope exhaustively, release during readiness polling (no VM
left, no overwritten state), the lost-race retry, owner/unknown 404s,
idempotent re-release, quota freed after the janitor pass, and the machine
list skipping disabled machines. Also against a real `uvicorn` process
(fake backend, janitor every 2s): list → mine → 404 for another user →
`202 DESTROYING` → janitor logs `DESTROYED` → my list is empty.

## M9 part 2 — the web frontend

`frontend/`: React + Tailwind client built with Vite, and a thin Fastify
server (`server/app.ts`) that serves it and is the only path to the
orchestrator. Node 24 runs the server's TypeScript directly (type
stripping), so only the client has a build step. Own Dockerfile
(multi-stage, `node:24-slim`, non-root) and a `lab-frontend` service in
`compose.yaml`: no published port, traefik labels whose router rule,
entrypoint, cert resolver, and authentik middleware come from `deploy/.env`
(I don't know the names in the VPS's traefik setup).

Screens: machine list → request → status with startup steps (polling every
2s while changing, 15s when ready) → ready view with remaining time and
"End session" → ended/failed view with the failure reason. The ready view
shows `ssh user@ip` as a placeholder until M11 adds the Guacamole session.

**Security boundary, as built:** `user` comes only from the identity
header (validated; a duplicated header is refused); with no header the
API answers 401 unless `LAB_FRONTEND_DEV_USER` is set (local dev only, never
in compose). Request bodies are schema-checked and unknown fields are
stripped, so a body can't choose the user. Instance ids must be ULIDs
before anything is forwarded. There is no generic proxy. Orchestrator
errors are mapped: 404 → 404, 400/409 → their message, anything else or
unreachable → 502 with a generic message.

**One more orchestrator change:** `GET /v1/instances/{id}` takes an optional
`?user=` and answers 404 for someone else's lease. Needed because the
frontend polls by id — a failed lease is `DESTROYED`, drops out of the
user's active list, and polling by id is how the failure reason still
reaches the user.

**Judgment calls:**
- `failure_reason` is shown to the user as-is. For a test deploy that's
  useful; it can contain internal details (tux2lab command text), so it may
  want a friendlier mapping before real users see it.
- Node.js 24 LTS was installed user-locally on the dev VM
  (`~/.local/node`, verified against nodejs.org's SHA-256 list), at the
  human's choice.

**Verified:** frontend — 31 tests (server boundary against a real stand-in
orchestrator over HTTP, state helpers, App flows in jsdom), type-check
clean, client builds. Orchestrator — 158 tests, `ruff` clean. End-to-end
locally: orchestrator under `uvicorn` (fake backend) + the frontend server
set up as in its image (production dependencies only, built client): page
and client routes served; 401 without the header; a body claiming
`"user":"mallory"` still created the lease for the header user (checked in
the DB); mallory gets 404 on it; release → `DESTROYING` → `DESTROYED`;
orchestrator down → 502. **Not verified:** the UI in a real browser (only
jsdom), the Docker images, and the M9 done-when behind traefik/authentik
on the VPS.

## M8 verification on a test VM (and M9 in containers)

A disposable test VM, `claude.hermann.internal` (Ubuntu 26.04, 2 vCPU,
1.9 GiB RAM, on the VPS's tux2lab lab), with its own `claude` account and
key (`~/.ssh/claude-testvm/` on the dev VM). Docker Engine 29.8.2 + Compose
v5.5.1 from Docker's apt repository. The human chose not to use the VPS's
traefik/authentik for this round, so the stack ran with
`deploy/compose.test.yaml`.

**Compose split (new):** `compose.yaml` is now a base without traefik labels
or published ports, plus exactly one override: `deploy/compose.traefik.yaml`
(labels, for the VPS) or `deploy/compose.test.yaml` (frontend on the
machine's `127.0.0.1:3000` only — nothing else on the network can reach it
to forge the identity header).

**Worth knowing:** tux2lab installs `/etc/ssh/ssh_config.d/999-tux2lab.conf`
on lab VMs, which silently adds the lab-wide key to every SSH connection to
a lab host. The first login to the test VM used that key without it being
asked for; the dedicated access now uses `ssh -F` with its own config so the
lab key is never offered.

**Verified on the test VM**, code shipped with `git archive` of the
committed tree:
- both images build (first real `docker build`); both containers healthy;
  together ~95 MB RAM
- only `127.0.0.1:3000` listens; ports 3000 and 8000 refuse connections
  from the lab network
- through the frontend container: page served; 401 without the header;
  machine list; request with a forged `"user":"mallory"` in the body → the
  lease belongs to `hermann` in the container's DB; mallory gets 404 on it;
  a second request → 409 with the quota message; release → `DESTROYING` →
  `DESTROYED`
- `docker compose down` + `up` keeps the DB
- `docker kill` while a lease was `WAITING_READY`, then `up`: `docker logs`
  shows `reconcile: instance … (user hermann, VM lab-m03-atlas-…) was
  WAITING_READY when the orchestrator stopped; moved to CLEANUP…`, and the
  janitor finished it within the same second (`failure_reason`: "orchestrator
  restarted while instance was WAITING_READY")
- janitor/reconcile INFO lines appear in `docker logs` (the M8 logging fix)

**Still open:** the M9 done-when behind traefik + authentik (needs the
VPS's instances: routing file or labels, an authentik application for the
test hostname, DNS), and the UI in a real browser.

---

## Open questions still outstanding

None blocking M9. Its stack is decided: React + Tailwind (Vite) with a thin
TypeScript Node.js server (Fastify or Express) that is the only thing
talking to the orchestrator — chosen over Next.js for a smaller, explicit
server-side boundary (implementation plan M9). Further M9 decisions,
delegated to me by the human and recorded in the docs: Fastify over
Express; three API additions (`GET /v1/machines`, `GET /v1/instances?user=`,
`DELETE /v1/instances/{id}?user=` as early release via a new
`USER_RELEASED` event, owner-checked); identity from `X-authentik-username`.
Items below use the numbering at the time of writing (M9 there = today's M10). Tracked in `ARCHITECTURE.md` §14: list/delete endpoints
(§14.7, revisit for M12), the tux2lab named-image feature (§14.8), the VM
login user (§14.9, verify in M9), fragile text parsing (§14.10), and SSH vs.
a graphical session for the tool-controller software (§14.11, needed before
M10). Short-label resolvability (§5) also gets checked on the real host in M9.

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
