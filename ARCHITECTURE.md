# Lab Orchestrator — Architecture & Decisions

**Status:** Requirements-gathering complete. Ready to move into concrete project design / implementation.
**Purpose of this document:** a single, self-contained reference of everything established during design discussion, written so an AI coding agent (or a human picking this up cold) has full context without needing to re-derive any of it.

---

## 1. What this system is

A small **orchestrator service** that leases ephemeral lab VMs to authenticated users. A user requests a "machine type" (a pre-defined image/software combo), the orchestrator provisions a VM through `tux2lab`, hands back connection details, and automatically tears the VM down after a lifetime cap or a disconnect grace period.

**Target user workflow** (test setup on the VPS `planetsexpress.dedyn.io`, see §17):

1. The user opens the site; **traefik** routes them, after **authentik** authentication, to the **web frontend** — a separate container that is part of this project (M9, not implemented yet).
2. The user requests a VM for a specific machine. Each machine type is backed by its own prefabricated golden image containing that machine's tool-controller software (§3).
3. The frontend calls the orchestrator, which provisions the VM through `tux2lab`.
4. Once the VM is `READY`, the user is handed a **Guacamole session** to it (§13).

The orchestrator API is never exposed outside the host's container network; the frontend is its only caller.

Non-goals for v1: multi-host scheduling, persistent/snapshot VMs, RDP (pending §14.11), true idle detection, a job queue, or any auth system of its own.

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
- **1 user → max 1 active `LabInstance`**, regardless of machine type. A second `POST` while one is active is rejected with `409` (decided in M5, see §14.6).
- **Host → max N active `LabInstance`s globally**, N = `LAB_ORCH_MAX_ACTIVE_INSTANCES` (default 3), sized to the host's free RAM (§17, "Capacity").
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

**One golden image per machine type.** `tux2lab_image` names a prefabricated golden image that already contains that machine's tool-controller software — selecting machine A installs image A. Today's `tux2lab` can only install golden images selected by distro + version (`vm install -d <distro> -v <version>`); installing from a *named* per-machine image is a planned `tux2lab` enhancement, developed separately. Until it exists, the host wrapper translates the image name into `-d/-v` (§11, §14.8). The orchestrator's config and code do not change when that enhancement lands.

**VM size lives on the host, not here.** The host's image map gives each image its vCPUs and RAM, and the wrapper passes them to `tux2lab vm install` as `--cpu/--memory` (§11). The orchestrator's key can't ask for a bigger VM than the host allows, and the orchestrator's interface (`install -H <host> -i <image>`) stays the same. Decided 2026-10-04.

## 4. Identity (prototype vs. final)

**Final:** `Browser → traefik + authentik (forward auth) → web frontend → orchestrator`. The web frontend is this project's own container (M9). It takes the authenticated username from authentik's forward-auth headers and passes it as `user`; the orchestrator never implements login/password auth itself, and its API stays unauthenticated but reachable only on the internal container network (§17). Because of that, the browser can never call the orchestrator directly: the frontend needs its own server side that calls the orchestrator by service name. The authentik header is trustworthy only because the frontend is reachable solely through traefik (no published port). Guacamole separately uses OIDC/SSO against authentik.

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

**Decision (resolved):** the username never appears in the VM name, and the orchestrator does not build or store a `.{username}.internal` suffix. `naming.py` generates only the short opaque label above and takes no user input. If `tux2lab`'s own DNS zoning appends a suffix, that is `tux2lab`'s concern and outside the orchestrator's contract. (It does: `tux2lab` expands a bare `-H` name to `<name>.<user>.internal` and reports VMs by that FQDN. The adapter maps FQDNs back to the short label — §11.) (Not yet verified against a real host: whether the short label resolves from the client's network, or whether the IP in the `GET` response is what users should connect to.)

## 6. Lifecycle state machine

```
REQUESTED
    │
    ▼
PROVISIONING        (tux2lab vm install)
    │
    ▼
STARTING
    │
    ▼
WAITING_READY        ├── VM_STATE == running
    │                ├── OS_STATE == healthy
    │                └── TCP/22 reachable
    ▼
READY
    │
    ▼
CONNECTED            (Guacamole tunnel open)
    │
    ├── reconnect ──────────────┐
    │                           │
    disconnect                  │
    │                           │
    ▼                           │
DISCONNECTED_GRACE (5 min)      │
    │                           │
    ▼                           │
DESTROYING ◀── expires_at reached (4h hard cap, from any state)
    │
    ▼
DESTROYED

Failure branch (from any state):
FAILED → CLEANUP → DESTROYED
```

Readiness is **stricter than "VM running"**:

```
READY = VM_STATE == running
        AND OS_STATE == healthy
        AND TCP/22 reachable
```

No full SSH login is needed for the readiness probe — a lightweight TCP/22 check is enough. `tux2lab vm list` / `vm info` / `vm validate` supply `VM_STATE`/`OS_STATE`.

The `FAILED → CLEANUP → DESTROYED` branch should exist in the state model from day one even though v1 doesn't need sophisticated recovery logic — it just needs to be a reachable state, not bolted on later.

## 7. Timers & cleanup policy (v1)

| Timer | Value | Status in v1 |
|---|---|---|
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

**Added for the web frontend (M9, decided):**

```
GET    /v1/machines                         → enabled machine types (machine_type, display_name)
GET    /v1/instances?user=<name>            → that user's active leases (0 or 1)
DELETE /v1/instances/{id}?user=<name>       → 202, early release; 404 unless <name> owns it
GET    /v1/instances/{id}?user=<name>       → as before, but 404 unless <name> owns it
```

The `?user=` on `GET /v1/instances/{id}` is optional (without it, the M5 behavior is unchanged); the frontend always sends it. It is needed because the frontend polls a lease by id: a lease that failed is `DESTROYED` and drops out of the user's active list, and polling by id is how the user still sees the failure reason.

Early release adds one state-machine event, `USER_RELEASED`: from any pre-destroy state to `DESTROYING`, exactly like `LIFETIME_EXPIRED` (§6). The janitor then removes the VM as it does for an expired lease. The API itself stays unauthenticated and internal (§4, §17); the ownership check is defense in depth behind the frontend's own check.

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
- **One active instance per user** — should be enforced at the DB level (e.g. a partial unique index on `user_id` where `state` is "active"), not just in application code, to avoid a race between two concurrent `POST`s. *(Resolved in M2: partial unique index on `user_id` where `state != 'DESTROYED'` — see §14.1.)*
- **Max 3 active instances globally** — same atomicity concern applies. *(Resolved in M2: a `BEFORE INSERT` trigger counting non-`DESTROYED` rows.)*

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

`tux2lab` is treated as a **black box**, accessed only through its CLI (upstream: `github.com/Muthukumar-Subramaniam/tux2lab`). The orchestrator uses five commands, always through the host wrapper below:

```
tux2lab vm install -H <hostname> -i <image>    # wrapper contract; see "interim mapping"
tux2lab vm list
tux2lab vm info -H <hostname>
tux2lab vm start -H <hostname>
tux2lab vm remove -H <hostname>                # wrapper adds -f
```

**Verified against the tux2lab source** (the original design assumed JSON output and an image flag; neither is true today):

- `vm install` selects a golden image by `-d <distro> -v <version>`, and prompts interactively if the choice is ambiguous. There is no image-name flag yet (§3).
- `vm list` and `vm info` print **ANSI-colored text** (a table and a tree), not JSON. `vm list` columns: `VM-Name VM-State OS-State OS-Distro`; `OS-State` is `healthy` when the guest's `systemctl is-system-running` reports `running`.
- `vm info -H` shows the IPv4/IPv6 addresses only when the VM is running *and* its SSH port answers.
- `vm remove` asks for confirmation unless given `-f`.
- VMs are named by **FQDN** (`lab-m01-aurora-7k4m2.hermann.internal`); a bare `-H` name is expanded to the lab domain.
- The CLI runs as the lab user and uses passwordless `sudo` internally (virsh).
- **Errors are printed to stdout, not stderr.** `print_error` and friends are plain `echo`s, so an error message and the exit code are the only failure signals; stderr is usually empty.
- **Colors are always on.** The ANSI codes are hard-coded and not disabled when there is no terminal, so every output the orchestrator receives contains them.
- **Prompts hang without a terminal.** The session has no stdin, so if a command ever reaches an interactive prompt (e.g. `vm install` with an ambiguous image choice), `read` gets end-of-input and the script loops until the orchestrator's timeout kills it. The wrapper must always pass every argument that would otherwise be asked for.
- **`vm install` also starts the VM** and returns while it boots (~1 minute per VM for golden images, per `tux2lab`). A following `vm start` only reports "already running". Cloning the disk can take longer than the 30s default command timeout; whether an install survives the SSH channel being closed mid-run is unverified.

**How a call works.** The orchestrator opens an SSH session to the host as `lab-orchestrator` and requests e.g. `tux2lab vm list`. Because the key is bound to a forced command, sshd runs the wrapper instead and hands it the requested text in `SSH_ORIGINAL_COMMAND`; the wrapper checks it and runs the real CLI on the host as the tux2lab user. The CLI's stdout, stderr, and exit code travel back over the same SSH channel. The adapter treats a non-zero exit as a command error and parses stdout into `VM`/`VMInfo` values; only those parsed values (state, IP, a failure message) reach the database. It is strictly request/response: `tux2lab` never calls the orchestrator, so the orchestrator polls (`vm info` during provisioning, `vm list` at startup).

**Interim mapping.** Until `tux2lab` can install a named golden image, the wrapper translates `-i <image>` to `-d <distro> -v <version>` using a host-side map file. Once the enhancement exists, only that wrapper line changes.

**Output parsing.** Because there is no machine-readable output, the adapter parses the text (ANSI stripped) and normalizes FQDNs to the short label the orchestrator stores. This is inherently fragile; a `--json` output flag is a candidate for the `tux2lab` enhancement (§14.10).

**More from the source (M10), and how the adapter reads it (decided):**

- `vm info -H` has **no OS state**: it prints `State: <virsh state>` for a stopped VM, `running (SSH not accessible)` while booting, and the full tree (with `IPv4` addresses as `a.b.c.d/prefix`) once SSH answers. Only `vm list` has the OS-State column. → `info()` reads `vm list` for power and OS state, and calls `vm info -H` for the IPv4 only when the row says `running` / `healthy`.
- `vm info -H` on an **unknown VM** prints `State: unknown` and **exits 0**. → "Not found" means the hostname is absent from `vm list`.
- `vm list`'s VM-State is virsh's first word (`running`, `shut` for "shut off", …); OS-State is `healthy`, `SSH-Not-Ready`, another systemctl state, or `[ N/A ]`.
- `vm start` on a running VM exits 0 ("already running"); `vm remove` on an unknown VM exits 0 ("does not exist"). The adapter checks `vm list` before `remove`, so `VMNotFoundError` keeps its meaning.
- Errors: the wrapper passes output through untouched; the adapter takes the message from stdout's `[ERROR]` lines plus the exit code. A wrapper refusal is exit 126 with the reason on stderr.

`deploy/host/stand-in/tux2lab` reproduces these five commands' output from the source for testing; the adapter's tests run the real wrapper in front of it. Output captured on the real host replaces the samples in `tests/fixtures/tux2lab/` once available.

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

**Wrapper design (built in this repo, `deploy/host/`, M10):**

- A dedicated host account `lab-orchestrator`. Its `authorized_keys` entry carries `restrict,command="<wrapper>"`, so the key can do nothing but run the wrapper — no shell, pty, or forwarding.
- The wrapper parses `SSH_ORIGINAL_COMMAND` and accepts only the five commands above, with arguments checked against the same strict hostname/image patterns the adapter enforces (§14.3). Anything else is rejected and logged.
- It adds `-f` to `remove`, applies the interim image mapping to `install` (distro/version plus the image's `--cpu/--memory`, §3), and runs the real CLI as the tux2lab user through a sudoers rule that permits exactly that one step.
- Before an install it checks the host's free RAM: if `MemAvailable` is below the VM's memory plus `RESERVE_MIB`, it prints `[ERROR] lab host is out of memory` and exits **75** without calling tux2lab (tux2lab itself only checks against *total* RAM). The adapter raises `HostCapacityError` for exit 75. It is never retried, and the lease ends `FAILED` with "The lab is full right now, please try again later."
- Every call is logged to the host's syslog.

## 12. SSH key handling

All VMs use `labuser@<vm>`. `tux2lab` has one lab-wide private key at `/tux2lab-data/lab-config/ssh-keys/`. *(To verify in M10: `tux2lab` itself logs into VMs as the lab admin user from its own deploy config; whether that is `labuser` — §14.9.)*

Decision: **copy** the key into a separate, orchestrator-owned location rather than mounting `tux2lab`'s original:

```
/tux2lab-data/lab-config/ssh-keys/
             │
             │ controlled copy
             ▼
/opt/lab-orchestrator/secrets/ssh/lab_id_ed25519   (chmod 600, mounted read-only)
```

The private key must **never** appear in: curl responses, the frontend, the database, logs, or the browser. This same credential is reused later as the Guacamole SSH `private-key` connection parameter (§13) — it stays inside Guacamole's encrypted JSON payload, never returned plaintext.

## 13. Guacamole integration (after M10; part of the target user flow)

After a lease is `READY`, the web frontend obtains a Guacamole session for it (M11) and embeds or redirects to it — this is how users reach their machine (§1). Guacamole's web UI is routed by traefik; `guacd` connects to the VM **by IP** on `labbr0`, which needs the same host firewall rule as the orchestrator's readiness check (§17). The SSH example below uses the hostname for readability; the deployed payload uses the lease's IP.

Whether the machines' tool-controller software needs a **graphical** session (RDP/VNC through Guacamole) rather than SSH is open (§14.11); the `protocol` field in machine definitions already allows for it.

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

This is more reliable than trying to infer browser-window closure from the frontend. All lifecycle *logic* stays in Python; the Java piece is purely an event bridge. Built after the core allocator (provision → ready → destroy, without Guacamole) works end-to-end against the real host (M10) — as M12.

**Guacamole ↔ authentik:** future integration will use Guacamole's OIDC extension for SSO, kept as a separate concern from the VM allocator.

## 14. Open items / risks not fully resolved in the conversation

These should be explicit decisions before/while implementing, not discovered mid-build:

1. ~~**Quota-check atomicity.**~~ — **resolved in M2** (DB-level partial unique index + `BEFORE INSERT` trigger, tested under concurrent writes). Original note: "One instance per user" and "max 3 global" were stated as rules but not as an enforcement mechanism. Recommend DB-level constraints (e.g. partial unique index) or an explicit transaction, not just an app-level `if` check, to avoid a race between two concurrent `POST /v1/instances`.
2. ~~**Restart reconciliation.**~~ — **resolved in M7** (`core/reconcile.py`): stuck leases are failed and cleaned up, drift against `tux2lab vm list` is logged, unknown VMs are never auto-removed. Original note: State surviving a restart (via SQLite) is not the same as state being *correct* after a restart. If the orchestrator dies mid-`vm install`, nothing currently reconciles the DB against `tux2lab vm list` on startup. Recommend an explicit reconciliation step at boot.
3. **Host-wrapper input validation.** The SSH-based host wrapper is a deliberate privilege boundary; hostnames/params crossing it need strict validation (e.g. a tight allowed-charset regex) to avoid command injection, since this is exactly the kind of boundary that's easy to under-specify.
4. **CLI call idempotency.** If a `tux2lab vm install` call times out on the orchestrator side without a definitive success/failure signal, a naive retry could double-provision. The adapter should check `vm info`/`vm list` before retrying a mutating call.
5. ~~**Hostname/DNS-suffix inconsistency**~~ — **resolved**, see §5: no username in the hostname, no orchestrator-built DNS suffix.
6. ~~**`POST` on existing active instance**~~ — **resolved in M5**: rejected outright with `409`, not returned as the existing instance. Reasoning in `api/routes_instances.py`'s module docstring.
7. ~~**List/delete endpoints**~~ — **decided for M9** (§8): `GET /v1/machines`, `GET /v1/instances?user=`, and `DELETE /v1/instances/{id}?user=` as early release via a new `USER_RELEASED` event. An admin-wide list remains out of scope.
8. **Named golden-image install in `tux2lab`** — external dependency (§3). Interim: the host wrapper maps image → distro/version. Nothing in the orchestrator changes when the enhancement lands.
9. **VM login user** — `labuser` (§12) vs. `tux2lab`'s own lab admin user. Verify on the real host in M10.
10. **No machine-readable `tux2lab` output** — the adapter parses colored text (§11). Fragile across `tux2lab` versions; a `--json` flag would remove the risk.
11. **Session protocol for tool-controller software** — SSH, or a graphical session (RDP/VNC) through Guacamole? Needed before M11.

## 15. Settled decisions (checklist)

- [x] One active VM per user; at most N active VMs globally (`LAB_ORCH_MAX_ACTIVE_INSTANCES`, default 3; was a fixed 3 until 2026-10-04)
- [x] Machine definitions live in orchestrator config, not tux2lab
- [x] tux2lab remains untouched; accessed only through its CLI
- [x] Orchestrator runs in its own container on the KVM host (details: §17)
- [x] Host-side restricted CLI bridge (SSH, allowlisted commands) for that container
- [x] Separate copy of the shared SSH key, read-only mount, never exposed to API/DB/logs/frontend
- [x] `labuser` is the VM account
- [x] VM hostnames are opaque: no username, no orchestrator-built DNS suffix (§5)
- [x] Asynchronous API: `POST` → `instance_id` → `GET` for status
- [x] SQLite, two tables (`machine_definitions`, `instances`)
- [x] No Redis/Celery/RabbitMQ/Kubernetes — single process + async janitor loop
- [x] Disposable VMs first; snapshots/persistence later (as a policy flag, not a separate system)
- [x] SSH first; RDP later (protocol field already in machine definitions)
- [x] Guacamole JSON-auth for ephemeral connection provisioning (M11)
- [x] Guacamole tunnel-close event as the eventual disconnect signal (M12)
- [x] v1 timers: 4h hard lifetime + 5m disconnect grace
- [x] 30m true-idle detection deferred to next iteration
- [x] One prefabricated golden image per machine type; interim image → distro mapping lives in the host wrapper (§3, §11)
- [x] Host wrapper is built in this repo (`deploy/host/`), dedicated `lab-orchestrator` account with a forced command (§11)
- [x] Orchestrator container on a Docker bridge network shared with the frontend and Guacamole; API never exposed outside it (§17)
- [x] Web frontend is part of this project, its own container (M9); users reach VMs through Guacamole (§1, §13)
- [x] Web frontend stack: React + Tailwind (Vite) served by a thin TypeScript Node.js server (Fastify) that alone talks to the orchestrator and sets `user` from the authentik header `X-authentik-username` (§4)
- [x] Users can release their VM early (`DELETE`, new `USER_RELEASED` event) so finished sessions free one of the global slots (§8)

## 16. Final architecture diagram

```
   Browser
      │ HTTPS (planetsexpress.dedyn.io)
      ▼
┌──────────────┐  forward auth  ┌───────────┐
│   traefik    │───────────────▶│ authentik │
└──┬────────┬──┘                └───────────┘
   │        │
   ▼        ▼
┌──────────────┐        ┌─────────────────────┐
│ web frontend │        │ Guacamole (web UI)  │
│   (M9)      │        │ + guacd             │
└──────┬───────┘        └──────────┬──────────┘
       │ REST (internal network)   │ SSH/RDP to VM IP
       ▼                           │
┌────────────────────────┐         │
│      ORCHESTRATOR      │  HTTP   │
│ FastAPI · Instance Mgr │────────▶│ (M11: JSON-auth tokens)
│ State Machine · Quota  │         │
│ Tux2Lab Adapter        │         │
│ Janitor · Reconcile    │         │
│ SQLite (/data volume)  │         │
└───────────┬────────────┘         │
            │ SSH (forced command)  │
            ▼                       │
┌────────────────────────┐          │
│ host wrapper           │          │
│ → tux2lab CLI (host)   │          │
└───────────┬────────────┘          │
            ▼                       │
┌────────────────────────┐          │
│ libvirt/KVM · labbr0   │◀─────────┘
│ tux2lab-engine (DNS,   │
│ DHCP, PXE)             │
└───────────┬────────────┘
            ▼
      ┌───────────┐
      │ Lab VM    │  one per lease, from the machine's golden image
      └───────────┘
```

## 17. Deployment (test setup)

Everything runs on one VPS (`planetsexpress.dedyn.io`, "the host"):

```
VPS (host)
├── containers on a shared user-defined bridge network ("lab network")
│   ├── traefik        public :443 → frontend, Guacamole web UI
│   ├── authentik      forward auth for traefik; OIDC for Guacamole
│   ├── web frontend   (M9) calls the orchestrator by service name
│   ├── orchestrator   no published port, no traefik route
│   └── guacamole      guacd connects to VM IPs on labbr0
├── tux2lab-engine     tux2lab's own container (Podman, host network): lab DNS/DHCP/PXE
├── tux2lab CLI        + deploy/host wrapper, reached over SSH by the orchestrator
└── libvirt/KVM        labbr0 (NAT, 10.28.28.0/22, domain <user>.internal) → lab VMs
```

- **Network.** The orchestrator joins the shared bridge network. Its API has no published port and no traefik route: only containers on that network (the frontend) can reach it. The orchestrator never talks to `tux2lab-engine` directly — only to the `tux2lab` CLI through the wrapper.
  - **Current VPS test setup (decided, temporary):** the "lab network" is traefik's existing `web` network, which the VPS's other apps (Seafile, Zammad, authentik, Guacamole, …) also join. Those containers can therefore reach the orchestrator API and the frontend directly, bypassing authentik and forging the identity header. Accepted while experimenting with the fake backend. The plan was to move the lab to its own network (only traefik, the frontend, the orchestrator and Guacamole) before real VMs; on 2026-10-04 the user deferred that, so M10 step B starts on `web` too. Still to do before the lab is used by anyone beyond testers.
- **Public route.** The frontend is served at `lab.planetsexpress.dedyn.io` (deSEC A record → the VPS) behind authentik forward auth (`authentik@file`, a Proxy provider in "Forward auth (single application)" mode). The hostname's `/outpost.goauthentik.io/` paths go to authentik's outpost through a second router with an explicit higher priority (README, "Deployment").
- **Host SSH.** The container reaches the host's sshd via `extra_hosts: host.docker.internal:host-gateway`. The host's sshd and the VPS firewall must accept connections from the Docker network's subnet. Host key checking stays on: a `known_hosts` file is mounted into the container (§11, `LAB_ORCH_TUX2LAB_SSH_KNOWN_HOSTS_PATH`).
- **VM reachability.** The readiness check (TCP/22) and `guacd` open *new* connections from the Docker network into `labbr0`. libvirt's NAT rules reject forwarded new connections into its network, so the host needs an explicit rule (e.g. in the `DOCKER-USER` chain) allowing the Docker network's subnet → `10.28.28.0/22`. Containers connect to VMs **by IP** — they don't use the lab DNS (10.28.28.1).
- **Persistence.** SQLite lives in a volume mounted at `/data` — the whole directory, since WAL mode keeps side files next to the DB. `machines.yaml` is mounted read-only. Secrets (the wrapper SSH key, `known_hosts`, later the lab VM key for Guacamole) are mounted read-only and never baked into the image (§12).
- **Process model.** Exactly **one** uvicorn worker. The janitor, the in-process provisioning tasks, and the SQLite locking model all assume a single process (§10). `docker stop` cancels in-flight provisioning; startup reconciliation cleans that up on the next start (§14.2).
- **Logs** go to stdout/stderr, for `docker logs`.
- **Capacity (decided 2026-10-04).** The lab VMs share the VPS with every other service, and RAM is the limit:
  - **CPU is shared freely.** A vCPU is an ordinary host thread; mostly-idle SSH VMs can have more vCPUs in total than the host has cores. Busy VMs only get slower.
  - **RAM is not overcommitted.** Count each VM at its full size plus ~150 MiB QEMU overhead; Linux guests fill free memory with cache over time. Running out lets the OOM killer pick a VM or a VPS service. KSM (merging identical pages) is an optional host setting (`deploy/host/README.md`).
  - **Disk.** An install copies the golden image (`qemu-img convert`), so a VM starts at the image's real size and grows up to 30 GiB (tux2lab's minimum). Leases last hours, so free space is watched, not budgeted.
  - **VM size:** 1 vCPU / 1 GiB (the minimum tux2lab allows for golden-image installs), set per image in the host's `images.conf`. Enough because the VMs are used over SSH only.
  - **Limit:** `LAB_ORCH_MAX_ACTIVE_INSTANCES` = floor((MemAvailable with no lab VMs running − 1 GiB reserve) / 1.15 GiB), from numbers measured on the VPS. The wrapper's RAM check (§11) is the safety net when something else takes RAM.
  - **Measured on the VPS (2026-10-04):** 8 threads (AMD EPYC-Milan, 4 cores × 2); 15,946 MiB RAM, **4,381 MiB available**, no swap; 133 GB free disk (`/tux2lab-data` is on `/`); KSM off. Already running: two VMs (`testvm2-ubuntu`, `claude`), and containers using ~8 GiB (largest: Zammad's Elasticsearch 1.6 GiB, authentik 1.5 GiB in total, Seafile 0.8 GiB). Golden images: almalinux 10 (2.3 GiB), rocky 9 (2.0 GiB), ubuntu-lts 26.04 (3.2 GiB).
  - **Result:** (4,381 − 1,024) / 1,178 = 2.8 → **limit 2**. CPU (8 threads for 1-vCPU VMs) and disk (~3 GiB per new VM out of 133 GB) are not the bottleneck. More slots need more free RAM, e.g. stopping a VM that isn't needed (a 2 GiB VM frees about two slots).

See `IMPLEMENTATION_PLAN.md` M8 (container packaging) and M10 (host wrapper + real `tux2lab`).
