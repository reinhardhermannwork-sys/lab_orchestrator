# Lab Orchestrator — Implementation Plan (For the Coding Agent)

**Read `ARCHITECTURE.md` first.** That file is the source of truth for *what* to build and *why*; this file is the *how* and *in what order*. Don't re-derive architecture decisions from scratch — if something here seems to conflict with that file, the architecture file wins, and the conflict should be flagged back to the human rather than silently resolved.

Target for v1 (backend core, M0–M9): a working `POST → poll → SSH manually` flow against a real tux2lab host, running as a container on the VPS. The **test-deploy target** builds on it (M10–M12): the full user workflow from architecture doc §1 — login through traefik/authentik, request a machine in the web frontend, work on it through a Guacamole session.

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
├── Dockerfile                       # M8: orchestrator image
├── .dockerignore
├── compose.yaml                     # M8: orchestrator service on the shared lab network
├── config/
│   └── machines.yaml
├── deploy/
│   ├── .env.example                 # M8: compose/env settings for the VPS
│   └── host/                        # M9: host-side restricted wrapper + setup docs
│       ├── lab-orchestrator-wrapper
│       ├── images.conf.example      # interim image -> distro/version map
│       └── README.md
├── frontend/                        # M12: web frontend (own container)
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
│       │   └── guacamole_client.py  # JSON-auth token builder (M10)
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

Work through these roughly in order. Each has a goal, what to build, and how to know it's done. Don't start Guacamole work (M10/M11) before M0–M9 are solid — the core allocator must work end-to-end against the real host first.

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

### M8 — Container packaging & stage-1 test deploy *(fake tux2lab)*
Package the orchestrator as a container and run it on the VPS per architecture doc §17, still against `FakeTux2LabClient`, to prove image, network, persistence, and restart behavior before touching the real host.
- `Dockerfile`: `python:3.12-slim`, non-root user, `pip install .` (non-editable), `WORKDIR /app`, default `config/machines.yaml` baked in (overridable by a read-only mount), `LAB_ORCH_DB_PATH=/data/orchestrator.db`, `uvicorn ... --host 0.0.0.0 --port 8000 --workers 1` (exactly one worker, §17), `HEALTHCHECK` against `/healthz` using Python (no curl in the image). `.dockerignore` keeps `.venv`, DB files, caches, `.git`, and secrets out.
- `compose.yaml`: one service on the external shared lab network (name from env); named volume at `/data`; read-only mounts for config and secrets; `extra_hosts: host.docker.internal:host-gateway`; `restart: unless-stopped`; a `stop_grace_period`; **no published port**. `deploy/.env.example` documents the variables.
- Logging to stdout, level from `LAB_ORCH_LOG_LEVEL` (default `INFO`), so janitor/reconciliation lines show in `docker logs`.
- Explicit backend selection: `LAB_ORCH_TUX2LAB_BACKEND` = `auto` (today's behavior: real client if SSH settings exist, else fake with a warning) | `fake` | `ssh` (startup fails if SSH settings are missing — no silent fallback in a deployment). Compose sets `fake` for this stage.
- **Done when**, on the VPS: the image builds and the container reports healthy; from another container on the lab network, `POST` → `GET` works and the lease reaches a terminal state (with the fake client that's normally the readiness-timeout failure path, since its placeholder IP isn't a real VM); `docker compose down && up` keeps the DB; `docker kill` mid-provisioning then `up` shows the reconciliation WARNING in `docker logs`; the API port is not reachable from outside the host.

### M9 — Host wrapper & real tux2lab integration *(stage 2)*
Wire the container to the real `tux2lab` CLI through the restricted wrapper (architecture doc §11) and make the adapter match the CLI's real behavior.
- `deploy/host/lab-orchestrator-wrapper` (bash): SSH forced command for the dedicated `lab-orchestrator` account. Parses `SSH_ORIGINAL_COMMAND`, accepts only `tux2lab vm {list | info -H h | start -H h | remove -H h | install -H h -i image}` with arguments matching the adapter's own patterns (`_HOSTNAME_RE`, `_IMAGE_NAME_RE` in `adapters/tux2lab_client.py`), rejects everything else, adds `-f` to `remove`, maps `-i <image>` to `-d/-v` via `/etc/lab-orchestrator/images.conf` (interim, §3), runs the CLI as the tux2lab user via a single sudoers rule, logs each call to syslog.
- `deploy/host/README.md`: account creation, the `authorized_keys` line (`restrict,command=...`), the sudoers entry, the host firewall rule (Docker network subnet → `labbr0` tcp/22, §17), and producing the `known_hosts` file mounted into the container.
- Adapter (`SSHTux2LabClient`): replace the JSON parsers with text parsers (ANSI stripped) for `vm list` (table → hostname, VM state, OS state) and `vm info -H` (state, IPv4); match "not found" to the real message; normalize tux2lab's FQDNs to the short label the orchestrator stores, so reconciliation and the janitor compare like with like. Parser tests use output **captured from the real host** (`tests/fixtures/tux2lab/`), not hand-written guesses.
- Verify and record: the VM login user (architecture doc §14.9) and whether users connect by IP or short name (§5).
- Compose switches to `LAB_ORCH_TUX2LAB_BACKEND=ssh`.
- **Done when**, from the container on the VPS: `POST` → `GET` reaches `READY` → `ssh <user>@<ip>` works; lease expiry removes the VM on the real host; `docker kill` mid-install then restart reconciles and the janitor removes the real VM; the wrapper refuses a non-allowlisted command (e.g. `vm stop`, `id`) when tried by hand with the orchestrator's key.

### M10 — Guacamole JSON-auth adapter *(after M9)*
- Decide the session protocol first (architecture doc §14.11: SSH vs. graphical RDP/VNC for the tool-controller software).
- Build the encrypted JSON payload and `/api/tokens` submission per architecture doc §13, connecting to the lease's **IP** (containers don't use the lab DNS, §17).
- The SSH private key must be injected directly into this payload from the secrets file, never passed through the DB or an intermediate API response.
- **Done when:** a generated token produces a working Guacamole session to a live instance.

### M11 — Guacamole tunnel-close listener *(after M10 — separate small Java project)*
- Minimal Guacamole extension using `TunnelCloseEvent` that `POST`s to `/internal/v1/guacamole/events`.
- Orchestrator-side: an internal endpoint that moves the referenced instance (via `guacamole_connection_id`) to `DISCONNECTED_GRACE`.
- **Done when:** closing a browser tab mid-session is observed to trigger the 5-minute grace countdown without any frontend polling.

### M12 — Web frontend *(own container, part of this repo)*
- Served through traefik behind authentik forward auth; reads the authenticated username from authentik's headers and passes it as `user` (architecture doc §4).
- Calls the orchestrator by service name on the shared lab network (never through traefik).
- Shows the enabled machine types, requests a VM, polls its status, and opens the Guacamole session once `READY`. Likely needs "my current instance" and early-destroy support in the API (§14.7).
- Tech stack is decided when this milestone starts.
- **Done when:** the test-deploy target checklist in §5 passes.

## 3. Cross-cutting concerns

- **Config/secrets:** all host paths, SSH connection details, and the secrets directory path come from environment variables / a settings module — never hardcoded.
- **Logging:** structured logs for every state transition (instance id, old state, new state, reason), written to stdout with the level from `LAB_ORCH_LOG_LEVEL` so they show in `docker logs` (M8). Never log the private key or full CLI commands containing secrets.
- **Error handling:** a failed provisioning attempt should land the instance in `FAILED`, not leave it stuck in `PROVISIONING` forever — this is what the `FAILED → CLEANUP → DESTROYED` branch exists for.

## 4. Testing strategy

- State machine: pure unit tests, no I/O.
- Tux2Lab adapter: run the same test suite against both the fake and (when available) the real client.
- Instance manager / API: integration tests against the fake `Tux2LabClient` and an in-memory or temp-file SQLite DB.
- Concurrency: explicit tests for the two race conditions called out in the architecture doc (§14, points 1 and 2) — don't consider quota enforcement done without them.
- Manual smoke test: the exact curl sequence from architecture doc §8, run against a real (or staging) tux2lab host before calling v1 complete.

## 5. Definition of done

### v1 — backend core (M0–M9)

- [ ] `POST /v1/instances` → `GET /v1/instances/{id}` → manual `ssh` works end-to-end against a real tux2lab host.
- [ ] Quota rules (1/user, 3/global) hold under concurrent requests, verified by test.
- [ ] 4h max lifetime and 5m disconnect-grace-adjacent cleanup logic work (disconnect-grace itself needs Guacamole, so for v1 this may only be testable via the max-lifetime path — note this gap rather than skipping the test silently).
- [ ] Startup reconciliation runs and surfaces drift.
- [ ] SSH private key never appears in an API response, log, or DB row — verified by grepping test output/logs, not just by code review.
- [x] The DNS-suffix / hostname question (architecture doc §5) has an explicit answer on record: opaque name, no username, no orchestrator-built suffix.
- [ ] The orchestrator runs as its container on the VPS (M8), with the API reachable only on the internal lab network.
- [ ] Lease expiry and startup reconciliation observed against the real host, not just the fake client (M9).

### Test-deploy target — full user workflow (M10–M12, builds on v1)

- [ ] A user logs in at `planetsexpress.dedyn.io` through traefik/authentik and reaches the web frontend.
- [ ] They request a machine and get a VM built from that machine's golden image.
- [ ] Once `READY`, they work on it through a Guacamole session.
- [ ] The VM is destroyed after the disconnect grace period or the 4h lifetime cap.
