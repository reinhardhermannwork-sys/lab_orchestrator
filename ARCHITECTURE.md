# Lab Orchestrator — Architecture & Decisions

**Status:** Requirements-gathering complete. Ready to move into concrete project design / implementation.
**Purpose of this document:** a single, self-contained reference of everything established during design discussion, written so an AI coding agent (or a human picking this up cold) has full context without needing to re-derive any of it.

---

## 1. What this system is

A small **orchestrator service** that leases ephemeral lab VMs to authenticated users. A user requests a "machine type" (a pre-defined image/software combo), the orchestrator provisions a VM through `tux2lab`, hands back connection details, and automatically tears the VM down after a lifetime cap or a disconnect grace period.

Non-goals for v1: multi-host scheduling, persistent/snapshot VMs, RDP, true idle detection, a job queue, or any auth system of its own.

## 2. Core concept: the instance lease

The one thing the orchestrator owns that `tux2lab` doesn't is the concept of a **lease**: an allocation of a VM to an authenticated user, for a given machine type, with a lifecycle and an expiry.

```
Authentik user
      │
      │ requests machine_1
      ▼
Orchestrator
      │
      │ allocate
      ▼
LabInstance
      │
      ├── machine definition = machine_1
      ├── owner              = user identifier (string for now; see §4)
      ├── vm_hostname         = generated (see §5)
      ├── state               = PROVISIONING / READY / ...
      ├── created_at
      ├── expires_at          = created_at + 4h
      └── disconnect_since
```

Rules:
- **1 user → max 1 active `LabInstance`**, regardless of machine type. A second `POST` while one is active is rejected (or returns the existing instance — decide this explicitly when writing the API layer; the conversation didn't pin down which).
- **Host → max 3 active `LabInstance`s globally.**
- `tux2lab` remains the sole authority over the actual VM; the orchestrator never manages VMs directly.

## 3. Machine definitions

Live in **orchestrator configuration**, not in `tux2lab`. `tux2lab` only knows how to provision VMs from an image name; the orchestrator knows which product/machine maps to which image.

```yaml
machines:
  machine_1:
    display_name: "Machine 1"
    code: "m01"
    codename: "aurora"
    tux2lab_image: "image_1_software_1"
    protocol: "ssh"
    enabled: true

  machine_2:
    display_name: "Machine 2"
    code: "m02"
    codename: "forge"
    tux2lab_image: "image_2_software_2"
    protocol: "ssh"
    enabled: true

  machine_3:
    display_name: "Machine 3"
    code: "m03"
    codename: "atlas"
    tux2lab_image: "image_3_software_3"
    protocol: "ssh"
    enabled: true
```

`code` and `codename` exist purely to build human-readable, "fancy" hostnames without leaking the username into VM identity.

## 4. Identity (prototype vs. final)

**Final:** `Browser → authentik → existing backend → orchestrator`. The orchestrator never implements login/password auth itself; when Guacamole is integrated it will separately use OIDC/SSO against authentik.

**Prototype (now):** the orchestrator is called directly with `curl`, so identity is passed explicitly:

```json
{
  "machine_type": "machine_1",
  "user": "hermann"
}
```

`user` is a plain string. Treat the DB column as a string, not a strict UUID type — that way swapping "explicit username" for "identity extracted from the authenticated request" later is a call-site change, not a schema migration.

## 5. VM naming

Format:

```
lab-<machine-code>-<codename>-<instance-suffix>
```

Example: `lab-m01-aurora-7k4m2`

- `instance-suffix` is a short random/Crockford-base32-style identifier so recycled or simultaneous instances never collide.
- The identifier is intentionally **opaque** — no username in the VM's own hostname component.

**Decision (resolved):** the username never appears in the VM name, and the orchestrator does not build or store a `.{username}.internal` suffix. `naming.py` generates only the short opaque label above and takes no user input. If `tux2lab`'s own DNS zoning appends a suffix, that is `tux2lab`'s concern and outside the orchestrator's contract. (Not yet verified against a real host: whether the short label resolves from the client's network, or whether the IP in the `GET` response is what users should connect to.)

---|---|---|
| Max lifetime | 4 hours | **Implemented** |
| Disconnect grace | 5 minutes | **Implemented** |
| Idle timeout | 30 minutes | **Deferred** to next iteration |

Rationale for deferring idle timeout: "user inactivity" is a genuinely different signal from "Guacamole tunnel closed," and would need real activity monitoring (keyboard/mouse/terminal traffic). For v1, activity is defined simply as:

```
active      = Guacamole tunnel is connected
disconnected = tunnel closed → enters 5-minute grace period → destroy if no reconnect
```

Timestamps that made it into the final schema: `created_at`, `ready_at`, `expires_at`, `disconnect_since`. (`last_activity_at` / `last_connected_at` were discussed as a four-clock model but were dropped for v1 along with idle detection — revisit together if idle timeout is implemented later.)

The **janitor** is a simple async background loop, e.g. every 10–15 seconds:

```
find instances where:
    now >= expires_at
    OR disconnected_for > 5 minutes
→ destroy them
```

Because timestamps live in SQLite, a container restart does not lose lifecycle state — but see §14 (open items) for what restart *doesn't* automatically fix.

## 8. REST API (v1, prototype)

Asynchronous by design — `POST` never blocks on provisioning.

**Allocate:**
```
POST /v1/instances
{
  "machine_type": "machine_1",
  "user": "hermann"
}

→ 202 Accepted
{
  "instance_id": "01K...",
  "machine_type": "machine_1",
  "state": "PROVISIONING"
}
```

**Poll:**
```
GET /v1/instances/{instance_id}

→ 200 OK
{
  "instance_id": "01K...",
  "machine_type": "machine_1",
  "machine_name": "Machine 1",
  "state": "READY",
  "hostname": "lab-m01-aurora-7k4m2",
  "ip": "10.28.28.42",
  "ssh": {
    "username": "labuser",
    "port": 22
  }
}
```

Manual curl workflow this enables (validated before Guacamole exists at all):
```bash
curl -X POST http://orchestrator:8000/v1/instances \
  -H 'Content-Type: application/json' \
  -d '{"user": "hermann", "machine_type": "machine_1"}'

curl http://orchestrator:8000/v1/instances/01K...

ssh -i /path/to/lab-private-key labuser@lab-m01-aurora-7k4m2   # or labuser@<ip>
```

Once Guacamole is integrated, the `READY` response drops raw SSH details in favor of:
```json
{ "state": "READY", "connection": { "protocol": "ssh", "guacamole_url": "..." } }
```

Endpoints implied but **not yet explicitly specified** in the conversation — decide during API design: `GET /v1/instances` (list, likely needed for the 3-global-max check and admin visibility), and whether a manual `DELETE /v1/instances/{id}` (early destroy) is in scope for v1.

## 9. Data model — SQLite

Single file, e.g. `orchestrator.db`. Two tables:

```
machine_definitions
--------------------
id
display_name
code
codename
tux2lab_image
protocol
enabled

instances
---------
id
user_id
machine_type
vm_hostname
vm_ip
state
created_at
ready_at
expires_at
disconnect_since
guacamole_connection_id     -- correlates a live Guacamole tunnel to this row;
                             -- used by the tunnel-close webhook (§12) to look
                             -- up which instance to move to DISCONNECTED_GRACE
destroyed_at
failure_reason
```

Constraints:
- **One active instance per user** — should be enforced at the DB level (e.g. a partial unique index on `user_id` where `state` is "active"), not just in application code, to avoid a race between two concurrent `POST`s. *(This wasn't explicitly resolved in the conversation — see §14.)*
- **Max 3 active instances globally** — same atomicity concern applies.

No PostgreSQL, no Redis. Rationale: modest host resources, tiny concurrency, explicitly a dev/eval system.

## 10. Process model — no Celery / Redis / RabbitMQ / K8s

One Python process/container:

```
FastAPI
  │
  ├── REST API
  ├── Instance Manager (state machine + quota rules)
  ├── Tux2Lab Adapter
  ├── Guacamole Adapter
  └── Janitor (async background loop)
```

## 11. Tux2lab integration

`tux2lab` is treated as a **black box**, accessed only through its documented CLI:

```
tux2lab vm install ...
tux2lab vm list
tux2lab vm info -H <hostname>
tux2lab vm start -H <hostname>
tux2lab vm remove -H <hostname>
```

Adapter boundary:

```python
class Tux2LabClient:
    async def install(self, hostname: str, image: str) -> None: ...
    async def list(self) -> list[VM]: ...
    async def info(self, hostname: str) -> VMInfo: ...
    async def start(self, hostname: str) -> None: ...
    async def remove(self, hostname: str) -> None: ...
```

Golden-image installation is the default fast provisioning path.

### Deployment topology (decided)

The orchestrator runs in **its own container**, separate from `tux2lab` (this was an explicit choice between "same host, direct exec" vs. "separate container" — separate container was chosen). Since the orchestrator container can't get privileged host access, it reaches the CLI over SSH to a **host-side restricted command wrapper**:

```
KVM HOST
┌────────────────────────────────────────────┐
│ tux2lab CLI                                 │
│      ▲                                      │
│ host-side restricted command wrapper        │
│      ▲                                      │
│      │ SSH                                  │
│ ┌────┴───────────────┐                      │
│ │ orchestrator        │                      │
│ │ container           │                      │
│ └────────────────────┘                      │
│ tux2lab-engine / libvirt/KVM / VMs           │
└────────────────────────────────────────────┘
```

- Dedicated host account + key, **allowlisted** wrapper — the orchestrator gets *approved commands only*, never general host shell access.
- No mounting of `/var/run/libvirt`, `/tux2lab-data`, or host root into the orchestrator container.
- `tux2lab` itself remains completely untouched — the only integration surface is its CLI.

## 12. SSH key handling

All VMs use `labuser@<vm>`. `tux2lab` has one lab-wide private key at `/tux2lab-data/lab-config/ssh-keys/`.

Decision: **copy** the key into a separate, orchestrator-owned location rather than mounting `tux2lab`'s original:

```
/tux2lab-data/lab-config/ssh-keys/
             │
             │ controlled copy
             ▼
/opt/lab-orchestrator/secrets/ssh/lab_id_ed25519   (chmod 600, mounted read-only)
```

The private key must **never** appear in: curl responses, the frontend, the database, logs, or the browser. This same credential is reused later as the Guacamole SSH `private-key` connection parameter (§13) — it stays inside Guacamole's encrypted JSON payload, never returned plaintext.

## 13. Guacamole integration (deferred until core allocator works)

**Approach chosen:** Guacamole's **encrypted JSON authentication** extension (`/api/tokens`), not persistent per-machine Guacamole DB connections. This fits the ephemeral-instance model — connections are generated on demand rather than pre-created and left around.

```json
{
  "username": "hermann",
  "expires": 1799999999999,
  "connections": {
    "Machine 1 — Aurora": {
      "protocol": "ssh",
      "parameters": {
        "hostname": "lab-m01-aurora-7k4m2",
        "port": "22",
        "username": "labuser",
        "private-key": "..."
      }
    }
  }
}
```

**Disconnect detection (deferred):** Guacamole's extension API exposes `TunnelConnectEvent` / `TunnelCloseEvent`. Plan is a small Guacamole Java extension that, on tunnel close, does:

```
Guacamole tunnel closes → listener → POST /internal/v1/guacamole/events → orchestrator
                                                                              │
                                                                              ▼
                                                          instance → DISCONNECTED_GRACE
```

This is more reliable than trying to infer browser-window closure from the frontend. All lifecycle *logic* stays in Python; the Java piece is purely an event bridge. **Explicitly deferred** until after the core allocator (provision → ready → destroy, without Guacamole) is working end-to-end.

**Guacamole ↔ authentik:** future integration will use Guacamole's OIDC extension for SSO, kept as a separate concern from the VM allocator.

## 14. Open items / risks not fully resolved in the conversation

These should be explicit decisions before/while implementing, not discovered mid-build:

1. **Quota-check atomicity.** "One instance per user" and "max 3 global" were stated as rules but not as an enforcement mechanism. Recommend DB-level constraints (e.g. partial unique index) or an explicit transaction, not just an app-level `if` check, to avoid a race between two concurrent `POST /v1/instances`.
2. **Restart reconciliation.** State surviving a restart (via SQLite) is not the same as state being *correct* after a restart. If the orchestrator dies mid-`vm install`, nothing currently reconciles the DB against `tux2lab vm list` on startup. Recommend an explicit reconciliation step at boot.
3. **Host-wrapper input validation.** The SSH-based host wrapper is a deliberate privilege boundary; hostnames/params crossing it need strict validation (e.g. a tight allowed-charset regex) to avoid command injection, since this is exactly the kind of boundary that's easy to under-specify.
4. **CLI call idempotency.** If a `tux2lab vm install` call times out on the orchestrator side without a definitive success/failure signal, a naive retry could double-provision. The adapter should check `vm info`/`vm list` before retrying a mutating call.
5. ~~**Hostname/DNS-suffix inconsistency**~~ — **resolved**, see §5: no username in the hostname, no orchestrator-built DNS suffix.
6. **`POST` on existing active instance** — rejected outright, or returns the existing instance? Stated informally as "rejected or return the existing instance" without a final call.
7. **List/delete endpoints** — not explicitly specified (see §8).

## 15. Settled decisions (checklist)

- [x] One active VM per user; at most 3 active VMs globally
- [x] Machine definitions live in orchestrator config, not tux2lab
- [x] tux2lab remains untouched; accessed only through its CLI
- [x] Orchestrator runs in its own container on the KVM host
- [x] Host-side restricted CLI bridge (SSH, allowlisted commands) for that container
- [x] Separate copy of the shared SSH key, read-only mount, never exposed to API/DB/logs/frontend
- [x] `labuser` is the VM account
- [x] VM hostnames are opaque: no username, no orchestrator-built DNS suffix (§5)
- [x] Asynchronous API: `POST` → `instance_id` → `GET` for status
- [x] SQLite, two tables (`machine_definitions`, `instances`)
- [x] No Redis/Celery/RabbitMQ/Kubernetes — single process + async janitor loop
- [x] Disposable VMs first; snapshots/persistence later (as a policy flag, not a separate system)
- [x] SSH first; RDP later (protocol field already in machine definitions)
- [x] Guacamole JSON-auth for ephemeral connection provisioning (deferred build)
- [x] Guacamole tunnel-close event as the eventual disconnect signal (deferred build)
- [x] v1 timers: 4h hard lifetime + 5m disconnect grace
- [x] 30m true-idle detection deferred to next iteration

## 16. Final architecture diagram

```
                         ┌───────────────┐
                         │   authentik   │
                         │   later/OIDC  │
                         └───────┬───────┘
                                 │
                                 ▼
                         Existing Backend
                                 │
                                 │ REST
                                 ▼
                    ┌────────────────────────┐
                    │      ORCHESTRATOR      │
                    │                        │
                    │ FastAPI                │
                    │ Instance Manager       │
                    │ State Machine          │
                    │ Access/Quota Rules     │
                    │ Tux2Lab Adapter        │
                    │ Guacamole Adapter      │
                    │ Janitor                │
                    │ SQLite                 │
                    └───────┬─────────┬──────┘
                            │         │
                     SSH/CLI│         │HTTP
                            ▼         ▼
                  ┌──────────────┐  ┌─────────────┐
                  │ host wrapper │  │ Guacamole   │
                  │ tux2lab CLI  │  │ guacd       │
                  └──────┬───────┘  └──────┬──────┘
                         │                 │ SSH
                         ▼                 │
                   ┌──────────┐            │
                   │ tux2lab  │◀───────────┘
                   │ / KVM    │
                   └────┬─────┘
                        ▼
                  ┌───────────┐
                  │ Lab VM    │
                  │ labuser   │
                  └───────────┘
```

**Next step:** turn this into the concrete Python project — package layout, state machine implementation, SQLite schema/migrations, REST contract, `Tux2LabClient` subprocess/SSH implementation, and exact Docker/host wiring. See the companion file `lab-orchestrator-implementation-plan.md`.
