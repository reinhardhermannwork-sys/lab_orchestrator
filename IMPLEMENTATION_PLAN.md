# Lab Orchestrator — Implementation Plan (For the Coding Agent)

**Read `lab-orchestrator-architecture.md` first.** That file is the source of truth for *what* to build and *why*; this file is the *how* and *in what order*. Don't re-derive architecture decisions from scratch — if something here seems to conflict with that file, the architecture file wins, and the conflict should be flagged back to the human rather than silently resolved.

Target for v1: a working `POST → poll → SSH manually` flow, with no Guacamole involved yet. Guacamole integration is explicitly a later phase.

---

## 0. Ground rules

- **No Celery/Redis/RabbitMQ/Kubernetes.** One Python process, FastAPI, SQLite, an `asyncio` background loop for cleanup. If you find yourself reaching for a job queue, stop — that's a sign of scope creep, not a missing requirement.
- **tux2lab is a black box.** Never touch its internals, its database, or its scripts directly. All interaction goes through its CLI, and in this deployment, through the host-side SSH wrapper described in the architecture doc (§11).
- **Secrets never leave the backend.** The SSH private key must never appear in an API response, a log line, or a DB column.
- Prefer small, independently testable modules over one large `main.py`.

## 1. Suggested project layout

```
lab_orchestrator/
├── pyproject.toml
├── README.md
├── config/
│   └── machines.yaml
├── src/
│   └── lab_orchestrator/
│       ├── __init__.py
│       ├── main.py                  # FastAPI app factory, startup/shutdown hooks
│       ├── api/
│       │   ├── routes_instances.py  # POST/GET /v1/instances
│       │   └── schemas.py           # Pydantic request/response models
│       ├── core/
│       │   ├── config.py            # env vars, paths, loaded machine defs
│       │   ├── state_machine.py     # states + legal transitions
│       │   └── instance_manager.py  # business logic: create/get/list/destroy
│       ├── db/
│       │   ├── models.py            # table definitions
│       │   ├── database.py          # connection/session handling
│       │   └── init_db.py           # schema creation (or alembic migrations)
│       ├── adapters/
│       │   ├── tux2lab_client.py    # SSH/subprocess adapter to the CLI
│       │   └── guacamole_client.py  # JSON-auth token builder (Phase 6+)
│       ├── janitor.py               # async background cleanup loop
│       └── naming.py                # hostname generation
└── tests/
    ├── test_state_machine.py
    ├── test_instance_manager.py
    ├── test_tux2lab_client.py       # against a fake/mock CLI
    └── test_api_instances.py
```

Suggested stack: **FastAPI + Pydantic v2**, **SQLite via SQLAlchemy Core** (or plain `sqlite3` — the schema is only two tables, a full ORM is optional), **`asyncssh`** for the async SSH calls to the host-side wrapper (keeps the adapter non-blocking without extra thread-pool plumbing).

## 2. Milestones

Work through these roughly in order. Each has a goal, what to build, and how to know it's done. Don't start Guacamole work (M6/M7) before M0–M5 are solid — the architecture doc is explicit that Guacamole is deferred until the core allocator works end-to-end.

### M0 — Scaffolding
- Repo skeleton above, `pyproject.toml`, dependency install, FastAPI app that boots and serves a health check.
- **Done when:** `uvicorn lab_orchestrator.main:app` starts and `GET /healthz` returns 200.

### M1 — Config & machine definitions
- `config/machines.yaml` loader (matches the format in the architecture doc §3).
- Validate on load: unique `code`/`codename`, required fields present, `enabled` respected.
- **Done when:** invalid config fails fast at startup with a clear error; valid config is queryable in-process (e.g. `get_machine("machine_1")`).

### M2 — Data layer
- Create the two tables exactly as specified in the architecture doc §9 (`machine_definitions`, `instances`).
- Add the **quota constraints as DB-level guarantees**, not just app-level checks:
  - a partial unique index (or equivalent) enforcing at most one "active" instance per `user_id`
  - a mechanism to atomically check-and-reserve the global max-3 slot (e.g. `SELECT COUNT(*) ... FOR UPDATE`-equivalent under SQLite's transaction semantics, or a single-writer serialization point)
- Write a small migration/init script rather than hand-editing the DB file.
- **Done when:** a test that fires two concurrent "create instance for the same user" calls proves only one succeeds — don't skip this test, it's covering the exact race the architecture doc flags as unresolved.

### M3 — State machine
- Implement the states and transitions from architecture doc §6 (`REQUESTED → PROVISIONING → STARTING → WAITING_READY → READY → CONNECTED → DISCONNECTED_GRACE → DESTROYING → DESTROYED`, plus `FAILED → CLEANUP → DESTROYED` reachable from any state).
- Keep this as a pure, dependency-free module: given a current state and an event, return the next state (or raise on an illegal transition). This makes it trivially unit-testable without touching the DB, tux2lab, or the network.
- **Done when:** every transition in the diagram has a test, and at least one illegal transition (e.g. `READY → PROVISIONING`) is proven to raise.

### M4 — Tux2Lab adapter
- Implement `Tux2LabClient` per architecture doc §11: `install`, `list`, `info`, `start`, `remove`, each invoking the CLI over the host-side SSH wrapper.
- Build a **fake/mock implementation** of the same interface for local development and tests that don't have a real KVM host available — this unblocks M5/M6 work in parallel.
- Validate/sanitize every value interpolated into a CLI invocation (hostnames, image names) against a strict allowed-charset before it crosses the SSH boundary — this is the injection-safety point flagged in the architecture doc §14.
- Before retrying any mutating call (`install`, `remove`) after a timeout/ambiguous failure, check current state via `info`/`list` first rather than blindly re-issuing — avoids double-provisioning.
- **Done when:** the fake client passes the same test suite as the real one (same interface), and a "malicious" hostname input is proven to be rejected before reaching the SSH call.

### M5 — Instance manager + REST API
- `instance_manager.py`: the orchestration logic — validate quota, generate hostname (`naming.py`, per architecture doc §5 — **resolve the DNS-suffix question first**, see below), call `Tux2LabClient.install`, drive the state machine through provisioning, poll for readiness (`VM_STATE == running AND OS_STATE == healthy AND TCP/22 reachable`), and persist state transitions to the DB.
- `POST /v1/instances` — validates request, enforces quota, creates a `REQUESTED` row, kicks off provisioning as a background task, returns `202` immediately with `instance_id` + `PROVISIONING` (per architecture doc §8).
- `GET /v1/instances/{id}` — returns current state; includes `hostname`/`ip`/`ssh` once `READY`.
- Before writing `naming.py`, get an explicit answer from the human on the open question in architecture doc §5 (does the FQDN legitimately include the username via tux2lab's DNS zoning, or should it not?) — don't guess silently on something explicitly flagged as unresolved.
- **Done when:** the full curl workflow from architecture doc §8 works against the fake `Tux2LabClient` end-to-end, and against the real one if a test host is available.

### M6 — Janitor
- Async background loop (10–15s interval per architecture doc §7): find instances where `now >= expires_at` or `disconnect_since` is more than 5 minutes old, transition them through `DESTROYING → DESTROYED`, and call `Tux2LabClient.remove`.
- **Done when:** an instance with a forced-past `expires_at` gets destroyed within one janitor cycle in a test.

### M7 — Startup reconciliation
- On boot, before serving traffic: reconcile DB state against `tux2lab vm list` (architecture doc §14, point 2). At minimum, detect and log/flag: DB says active but VM doesn't exist; VM exists but DB has no matching row; instance stuck mid-transition from a previous crash.
- This doesn't need a fully automated fix in v1 — surfacing the drift clearly is enough — but it must not be silently skipped.
- **Done when:** killing the process mid-provision and restarting produces a clear reconciliation log entry rather than a stuck/ghost instance.

### M8 — Guacamole JSON-auth adapter *(deferred — start only after M0–M7 are solid)*
- Build the encrypted JSON payload and `/api/tokens` submission per architecture doc §13.
- The SSH private key must be injected directly into this payload from the secrets file, never passed through the DB or an intermediate API response.
- **Done when:** a generated token produces a working Guacamole SSH session to a live instance.

### M9 — Guacamole tunnel-close listener *(deferred — separate small Java project)*
- Minimal Guacamole extension using `TunnelCloseEvent` that `POST`s to `/internal/v1/guacamole/events`.
- Orchestrator-side: an internal endpoint that moves the referenced instance (via `guacamole_connection_id`) to `DISCONNECTED_GRACE`.
- **Done when:** closing a browser tab mid-session is observed to trigger the 5-minute grace countdown without any frontend polling.

## 3. Cross-cutting concerns

- **Config/secrets:** all host paths, SSH connection details, and the secrets directory path come from environment variables / a settings module — never hardcoded.
- **Logging:** structured logs for every state transition (instance id, old state, new state, reason). Never log the private key or full CLI commands containing secrets.
- **Error handling:** a failed provisioning attempt should land the instance in `FAILED`, not leave it stuck in `PROVISIONING` forever — this is what the `FAILED → CLEANUP → DESTROYED` branch exists for.

## 4. Testing strategy

- State machine: pure unit tests, no I/O.
- Tux2Lab adapter: run the same test suite against both the fake and (when available) the real client.
- Instance manager / API: integration tests against the fake `Tux2LabClient` and an in-memory or temp-file SQLite DB.
- Concurrency: explicit tests for the two race conditions called out in the architecture doc (§14, points 1 and 2) — don't consider quota enforcement done without them.
- Manual smoke test: the exact curl sequence from architecture doc §8, run against a real (or staging) tux2lab host before calling v1 complete.

## 5. Definition of done for v1

- [ ] `POST /v1/instances` → `GET /v1/instances/{id}` → manual `ssh` works end-to-end against a real tux2lab host.
- [ ] Quota rules (1/user, 3/global) hold under concurrent requests, verified by test.
- [ ] 4h max lifetime and 5m disconnect-grace-adjacent cleanup logic work (disconnect-grace itself needs Guacamole, so for v1 this may only be testable via the max-lifetime path — note this gap rather than skipping the test silently).
- [ ] Startup reconciliation runs and surfaces drift.
- [ ] SSH private key never appears in an API response, log, or DB row — verified by grepping test output/logs, not just by code review.
- [ ] The DNS-suffix / hostname question (architecture doc §5) has an explicit answer on record, not an assumption baked into `naming.py`.

Guacamole (M8/M9) is **out of scope for "v1 done"** — it's the deliberately deferred next phase once the above is solid.
