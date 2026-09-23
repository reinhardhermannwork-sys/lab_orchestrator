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
- [ ] M1 — Config & machine definitions
- [ ] M2 — Data layer (quota constraints at the DB level)
- [ ] M3 — State machine
- [ ] M4 — Tux2Lab adapter (+ fake client for tests)
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
