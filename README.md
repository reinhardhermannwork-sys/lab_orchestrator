# Lab Orchestrator

A small service that leases ephemeral lab VMs to authenticated users. A user
requests a machine type, the orchestrator provisions a VM through `tux2lab`,
hands back connection details, and tears the VM down after a lifetime cap or
a disconnect grace period.

Full design context lives in `lab-orchestrator-architecture.md` (source of
truth for *what* and *why*) and `lab-orchestrator-implementation-plan.md`
(*how* and *in what order*) — keep both alongside this repo.

v1 target: a working `POST → poll → SSH manually` flow, no Guacamole yet.

## Stack

- FastAPI + Pydantic v2
- SQLite via SQLAlchemy Core
- `asyncssh` for the async SSH calls to the host-side tux2lab CLI wrapper
- One process, no Celery/Redis/RabbitMQ/Kubernetes — an `asyncio` background
  loop (the "janitor") handles cleanup

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run it:

```bash
uvicorn lab_orchestrator.main:app --reload
curl http://localhost:8000/healthz
# {"status":"ok"}
```

Run tests:

```bash
pytest
```

## Configuration

Settings (`core/config.py`) are env-var driven — `LAB_ORCH_<FIELD_NAME>`,
uppercased, or a `.env` file in the repo root. Currently defined:

| Env var | Default | Consumed starting |
|---|---|---|
| `LAB_ORCH_MACHINES_CONFIG_PATH` | `config/machines.yaml` | M1 |
| `LAB_ORCH_DB_PATH` | `orchestrator.db` | M2 |
| `LAB_ORCH_SECRETS_DIR` | `/opt/lab-orchestrator/secrets` | M8 |
| `LAB_ORCH_TUX2LAB_SSH_HOST` | *(none — required)* | M4/M5 |
| `LAB_ORCH_TUX2LAB_SSH_PORT` | `22` | M4/M5 |
| `LAB_ORCH_TUX2LAB_SSH_USERNAME` | *(none — required)* | M4/M5 |
| `LAB_ORCH_TUX2LAB_SSH_KEY_PATH` | *(none — required)* | M4/M5 |
| `LAB_ORCH_TUX2LAB_SSH_KNOWN_HOSTS_PATH` | *(none — see note below)* | M4/M5 |
| `LAB_ORCH_TUX2LAB_SSH_COMMAND_TIMEOUT` | `30.0` | M4/M5 |

Note the derivation is `LAB_ORCH_` + the field name uppercased, not an
abbreviation — e.g. `machines_config_path` → `LAB_ORCH_MACHINES_CONFIG_PATH`,
not `LAB_ORCH_MACHINES_CONFIG`. Worth double-checking against the field
name in `core/config.py` before relying on a new one.

## Tux2Lab adapter notes

`SSHTux2LabClient` (`adapters/tux2lab_client.py`) has to guess at two
things the architecture doc doesn't specify, because the host-side
wrapper doesn't exist yet for this codebase to inspect against:

1. **Wrapper command syntax** — assumed to mirror tux2lab's own CLI
   exactly (`tux2lab vm install -H <hostname> -i <image>`, etc.), since
   that's the only syntax the architecture doc documents. `install`'s
   flags are elided with "..." in the doc, so `-H`/`-i` is this
   codebase's own extrapolation from `info`/`start`/`remove`'s `-H`
   convention.
2. **`vm list`/`vm info` output format** — assumed JSON. "VM not found"
   detection in `info()` is a best-effort heuristic on stderr text
   (`"not found"`, `"does not exist"`, etc.), since there's no documented
   distinct signal for it. This means `install_idempotent`/
   `remove_idempotent`'s retry-safety is fully reliable against
   `FakeTux2LabClient` today, but may not reliably detect "already
   gone"/"not yet created" against a real host until the actual
   not-found signal is confirmed.

Both are isolated in small, clearly-marked functions specifically so
they're a small, obvious edit once a real host wrapper exists to test
against — nothing else in the codebase depends on their exact shape.

What *is* genuinely verified, not assumed: the SSH mechanics themselves.
`tests/test_tux2lab_client.py` runs a real local `asyncssh` server (key
generation, auth, command execution, timeout handling) standing in for
the wrapper, so connection handling, command construction, timeout
propagation, and error mapping are all tested against real SSH, not
mocks — only the wrapper's specific CLI dialect remains unverified.

`known_hosts` defaults to *not* being passed to `asyncssh.connect()` at
all (asyncssh's own default: check `~/.ssh/known_hosts`, fail closed if
there's no entry) rather than the more obvious-looking
`known_hosts=None`, which actually **disables host-key verification
entirely** — a real MITM exposure, never this codebase's default.
Set `LAB_ORCH_TUX2LAB_SSH_KNOWN_HOSTS_PATH` to point at a specific file
instead of relying on the container's home directory having one.

## State machine notes

`core/state_machine.py` encodes two judgment calls the architecture
doc's diagram doesn't fully settle on its own — both documented in the
module itself, summarized here:

- **`reconnect` direction.** The diagram's ASCII art draws it ambiguously
  (a line trailing off without a closed arrowhead). §7's prose
  ("destroy if no reconnect") settles it: reconnecting during the grace
  period returns to `CONNECTED`, it doesn't lead deeper into teardown.
- **Scope of "from any state."** For the 4h lifetime cap and the failure
  branch, "any state" is read as *any pre-destroy state* — not literally
  every state, which would produce nonsense like `DESTROYING`
  re-triggering itself, or an edge out of `DESTROYED` (which must have
  none — db/models.py's quota trigger depends on that).

## Data layer notes

- **Sync engine + stdlib `sqlite3` driver**, not `aiosqlite`. The
  quota trigger and partial unique index (below) lean on SQLite's
  ordinary single-writer locking, which is simplest to reason about
  synchronously; M5 will wrap DB calls from async routes in a
  thread-pool executor rather than switching drivers.
- **`machine_definitions` is a live sync target, not just a mirror of
  the yaml** — `instances.machine_type` is a real FK into it (FK
  enforcement is off by default in SQLite; `db/database.py` turns it on
  per connection). Startup upserts every machine from `machines.yaml`
  into the table; removing a machine from the yaml does *not* delete
  its row, since a past instance may still reference it.
- **Quota enforcement is DB-level, not app-level**: a partial unique
  index (`user_id` where `state != 'DESTROYED'`) for one-active-per-user,
  and a `BEFORE INSERT` trigger counting non-`DESTROYED` rows for
  max-3-active-globally. Both were verified against real concurrent
  writes (threads + a `Barrier`, not just sequential calls), per the
  implementation plan's explicit "don't skip this test" instruction.

## Project layout

```
src/lab_orchestrator/
├── main.py                  # FastAPI app factory, startup/shutdown hooks
├── api/
│   ├── routes_instances.py  # POST/GET /v1/instances
│   └── schemas.py           # Pydantic request/response models
├── core/
│   ├── config.py            # env vars, paths, loaded machine defs
│   ├── state_machine.py     # states + legal transitions
│   └── instance_manager.py  # business logic: create/get/list/destroy
├── db/
│   ├── models.py            # table definitions
│   ├── database.py          # connection/session handling
│   └── init_db.py           # schema creation
├── adapters/
│   ├── tux2lab_client.py    # SSH/subprocess adapter to the CLI
│   └── guacamole_client.py  # JSON-auth token builder (deferred)
├── janitor.py                # async background cleanup loop
└── naming.py                 # hostname generation
```

Every non-`main.py` module above is currently a stub — a docstring
describing its scope and which milestone fills it in, nothing more. That's
intentional: M0 is scaffolding only.

## Milestone status

- [x] **M0 — Scaffolding.** Repo skeleton, `pyproject.toml`, app boots,
      `GET /healthz` returns 200 (see `tests/test_main.py`).
- [x] **M1 — Config & machine definitions.** `Settings` (env-var driven)
      and the `config/machines.yaml` loader/validator, wired into the
      app's startup lifespan so invalid config fails fast — verified
      against a real `uvicorn` process, not just `TestClient` (see
      `tests/test_config.py`, `tests/test_startup.py`).
- [x] **M2 — Data layer.** `machine_definitions` + `instances` tables
      (SQLAlchemy Core), FK from `instances.machine_type` enforced via
      `PRAGMA foreign_keys=ON`, a CHECK constraint on `state`, and the
      two DB-level quota guarantees as real constraints (not app-level
      `if` checks): a partial unique index for one-active-per-user, and
      a `BEFORE INSERT` trigger for max-3-active-globally. Both verified
      under genuine concurrent writes with threads + a `Barrier`, run 15x
      to rule out flakiness (see `tests/test_db_models.py`).
- [x] **M3 — State machine.** `Event` enum + `next_state()` in
      `core/state_machine.py` — pure, stdlib-only. Every legal transition
      in the diagram is tested explicitly (not derived from the module's
      own table), plus an exhaustive sweep proving every *other*
      (state, event) pair raises — not just one hand-picked illegal
      example (see `tests/test_state_machine.py`).
- [x] **M4 — Tux2Lab adapter.** `Tux2LabClient` (abstract, matches
      architecture doc §11's 5-method interface exactly), `FakeTux2LabClient`
      (in-memory, what M5/M6 develop against day to day), and
      `SSHTux2LabClient` (real, `asyncssh`-based). Shared input validation
      on the base class so both implementations enforce the same
      allowed-charset check before anything crosses the SSH boundary.
      `install_idempotent`/`remove_idempotent` implement §14.4's
      check-before-retry requirement. Tested against a **real local SSH
      server** standing in for the host wrapper, not just mocks — see
      `tests/test_tux2lab_client.py` and the "Real-host assumptions"
      note below.
- [ ] M5 — Instance manager + REST API
- [ ] M6 — Janitor
- [ ] M7 — Startup reconciliation
- [ ] M8 — Guacamole JSON-auth adapter *(deferred)*
- [ ] M9 — Guacamole tunnel-close listener *(deferred, separate Java project)*

## Open questions carried over from the design docs

These are flagged in the architecture doc and **not yet decided** — don't
let an implementation detail silently answer them:

1. **DNS suffix in the VM hostname** (architecture doc §5, §14.5). Does
   `lab-m01-aurora-7k4m2.hermann.internal` legitimately reintroduce the
   username via tux2lab's DNS zoning, or does that conflict with the
   "opaque, no-username" hostname goal? Must be answered before `naming.py`
   (M5) is written.
2. **`POST` on an existing active instance** (§14.6): reject outright, or
   return the existing instance? Decide during M5 API design.
3. **List/delete endpoints** (§14.7, §8): is `GET /v1/instances` (list) or
   `DELETE /v1/instances/{id}` in scope for v1? Decide during M5.

A few other risks are tracked as concrete "done when" test requirements
rather than open questions — see implementation plan §M2 (quota race) and
§M4 (CLI injection-safety, retry idempotency).

## Notes on dependency choices made during scaffolding

- **SQLAlchemy Core over plain `sqlite3`**: the implementation plan leaves
  this as an explicit either/or. Core gives schema/constraint tooling
  (needed for the M2 partial-unique-index quota work) without pulling in
  full ORM machinery.
- **`python-ulid`** added for instance IDs, matching the `01K...` style ID
  in the architecture doc's API examples (§8).
