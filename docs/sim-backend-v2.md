# Simulation backend v2: many machines, state machines, cost per requestor (M14)

Status: **design draft D0, 2026-11-02, for the user's review.** Nothing built. Planned with the user in plan mode
(ARCHITECTURE §19 points here). Builds on M13 (ARCHITECTURE §18, mercury-lab `docs/sim-host.md`).

## 1. Why

M13 made this orchestrator the simulation backend. A lease is one simulated machine session (MERCSC0000: Mercury
under Wine + its own msim) and runs are recipes inside it. Using it on the shared test VM showed the gaps:

- nobody can see who holds which sim (`GET /v1/instances` needs a user or a case; there's no list of everything);
- there is no priority: dev/debug work can't push a check aside;
- a run can't be paused or stopped and resumed;
- an idle lease blocks one of the two slots for 4 h;
- "lab full" comes from the lagging 1-minute load average, not from what is actually reserved.

The user set three ground rules for the next design (2026-11-02):

1. **Many machines.** The backend simulates different machines with different hardware and software
   configurations, not only MERCSC0000 v1.2.
2. **Everything is a state machine:** leases, runs and every other part, explicitly modelled.
3. **Every simulation has a cost**, and the cost of a run is booked to its **requestor**.

ls/ps, priority and stop/resume are then built on top of these three.

## 2. Decisions (user, 2026-11-02)

| # | Decision |
|---|---|
| 1 | **Cost bearer = an account per case or project** (a service case, an internal dev project, an automatic check). The user is only the actor; the account pays. As Slurm's `--account`. |
| 2 | **One cost metric first: sim time = lease wall time**, counted in **sim-minutes**, from the lease request until DESTROYED (boot and idle time included: holding a slot costs). Run times are kept as a breakdown, not charged separately. No rate card, no EUR yet. |
| 3 | **Budget used up = hard stop for new starts.** Running leases and runs finish; new leases on that account are refused until the budget is raised. |
| 4 | **Many families in the design** (Mercury, Torrent/B&R ARsim, …), each with its own host CLI behind the provider interface. **Build only Mercury** with several configurations first (MERCSC0000, FSI114–116). |

## 3. Machines: layers, resolved into a pinned build

Today `image: mercsc0000-v1.2` bundles everything into one name (host `mercury-images.conf`: mercury-reference tag +
Wine image + msim image), and a machine setup comes along as `setup_json`. v2 separates the layers:

| Layer | Mercury example | Later family example | Lives in |
|---|---|---|---|
| **Family** = provider + host CLI | `mercury-sim` (Wine + msim) | `torrent-sim` (B&R ARsim) | orchestrator code + host CLI |
| **Hardware configuration** (the twin) | MERCSC0000, FSI114, FSI115, FSI116: plant description, options, I/O, chemicals | … | mercury-reference / knowledge base |
| **Software configuration** | Mercury version, Wine image `mercury-wine:w3-w11s4`, Root.xml, NVM, recipe set | … | mercury-reference tag |
| **Plant model** | msim `r9`, plant (FMU) version | … | mercury-sim |
| **Setup overlay** | the customer's config files or a case's changes (Fab Suite machine-config pane) | … | the request (case) |

- **Machine spec** (catalogue entry): family + hardware + software + plant, each by name and version. What a user
  picks.
- **Build**: a spec resolved to exact versions (tags → commits/digests) plus the hash of the overlay.
  Content-addressed: `build_id = sha256(family, hw@rev, sw@rev, plant@rev, overlay)`. The same build always gives
  the same machine, and every lease and run records its build, so "what exactly was simulated" is always known.
  Runs are repeatable (mercury-sim LS4), and this is what makes a rerun mean "the same thing again".
- **Resource profile per build** (measured, not guessed): RAM, CPU, disk per lease, boot time, typical run times
  per recipe. Used for placement (§5.5) and for the time estimate before start (§6). The first profile comes from
  the SP6 measurement: ~0.9 GB RAM, 0.75–1 CPU, READY ~1 min, WASHDOWN 68 s.
- The host still resolves names to real images (as today; the orchestrator's key can't ask for things the host
  doesn't allow). The build id is passed to the host CLI and recorded in `lease.json`.

Catalogue sketch (`config/catalogue.yaml`, replaces the per-machine `image` field; `machines.yaml` stays readable
for tux2lab VMs):

```yaml
families:
  mercury-sim:  {provider: mercury-sim, protocol: vnc, probe_port: 5900}
  # torrent-sim: {provider: torrent-sim, protocol: vnc, probe_port: …}   # later

hardware:            # the twins
  mercsc0000: {family: mercury-sim, source: "mercury-reference:twins/MERCSC0000"}
  fsi114:     {family: mercury-sim, source: "mercury-reference:twins/FSI114"}

software:
  mercury-v1.2: {family: mercury-sim, mref: "v1.2", wine: "mercury-wine:w3-w11s4"}

plants:
  msim-r9: {family: mercury-sim, image: "msim:r9"}

specs:               # what a user picks
  mercsc0000-v1.2: {hardware: mercsc0000, software: mercury-v1.2, plant: msim-r9, lifetime_hours: 4}
  fsi114-v1.2:     {hardware: fsi114,     software: mercury-v1.2, plant: msim-r9, lifetime_hours: 4}
```

Hosts declare capacity and families (`config/hosts.yaml`): the test VM = `mercury-sim`, 2 slots (from SP6). A later
host may add `torrent-sim`.

## 4. Everything is a state machine

### 4.1 The pattern

Today's `core/state_machine.py` stays the model: a pure transition table `(state, event) → state`, no I/O,
`IllegalTransition` for anything else. v2 adds:

- **One table per entity** (`core/fsm/lease.py`, `run.py`, `request.py`, `account.py`, `build.py`, `host.py`), all
  in the same form.
- **One transition function** (`core/fsm/engine.py: transition(entity, event, actor, reason)`): checks the table,
  checks the guards (§4.3), updates the entity row and appends a row to the **transition log**, all in one DB
  transaction. Nothing else may write a `state` column.
- **Transition log** (append-only table `transitions`: `id, entity_kind, entity_id, from_state, to_state, event,
  actor, reason, at`). It answers "who stopped my sim and why", it is the audit, and **it is the source of the cost**
  (§6: time between two log rows).
- **Diagrams are generated** from the tables (Mermaid) into this doc, so code and doc can't drift.
- No state-machine library: python-statemachine 3.x has compound states and history, but no SQLAlchemy binding and
  one DB write per state entered. Our own tables are simpler and already tested.

### 4.2 Entities

States marked *(new)* come with the step that uses them (§8); D1 ports only today's states.

**Request (admission), new.** What a user or case asks for, before a lease exists.

```
SUBMITTED ──resolve──▶ RESOLVED ──admit──▶ ADMITTED ──lease_created──▶ BOUND
     │                    │  └──no_slot──▶ QUEUED ──slot_free──▶ ADMITTED      (D6)
     │                    └──budget_exhausted / no_such_spec──▶ REJECTED
     └──cancel (from SUBMITTED/RESOLVED/QUEUED)──▶ CANCELLED
```

Until D6 there's no queue: "no slot" → REJECTED "the lab is full" (as today's 409/75).

**Lease (machine session).** Today's §6, plus:

| From | Event | To | Step |
|---|---|---|---|
| READY / CONNECTED | `SUSPEND` (docker pause: CPU freed, RAM held) | SUSPENDED *(new)* | D5 |
| SUSPENDED | `RESUME` | READY | D5 |
| READY / CONNECTED / SUSPENDED | `PREEMPT` | DESTROYING (reason = preempted by …) | D6 |
| READY (no run for the idle limit) | `IDLE_EXPIRED` | DESTROYING | D4 |

The lifetime clock (4 h) keeps running in SUSPENDED (wall time is the cost, §6).

**Run (a recipe inside a lease).** Today `queued | running | done | failed` (host-reported). v2:

```
QUEUED ─▶ RUNNING ─▶ DONE
  │          │ ├──▶ FAILED
  │          │ ├──abort──▶ ABORTED                       (D5)
  │          │ └──pause──▶ PAUSED ──resume──▶ RUNNING     (D5, lockstep pause; run gets fidelity = "paused")
  │          └──preempt──▶ PREEMPTED ──requeue──▶ QUEUED (D6, a rerun from the start, new attempt no.)
  └──cancel──▶ ABORTED
```

Pause uses the existing lockstep control (msim `clock pause | paced`, mercury-sim LS2). Known limits
(mercury-sim docs/lockstep.md §8.6/§11.2): wall-clock items inside Mercury keep running (cover/index timeouts 30 s,
heater watchdog 2 s), so a paused run is marked **fidelity = paused** and not used as a reference. A clean "resume"
after a long stop is a requeue (rerun from start).

**Account (cost bearer), new.**

```
ACTIVE ──usage ≥ warn level──▶ WARNED ──usage ≥ budget──▶ EXHAUSTED
   ▲                              │                           │
   └────────── budget_raised ─────┴───────────────────────────┘
any ──close──▶ CLOSED (no new requests; history kept)
```

EXHAUSTED refuses new requests (decision 3); running leases go on and their minutes are still booked (the account
can go below zero; that shows up in the report).

**Charge (one lease's cost), new.**

```
OPEN (lease running, minutes accrue) ──lease DESTROYED──▶ SETTLED ──export──▶ BOOKED
   └──operator void (with reason)──▶ VOIDED
```

**Build, new (D2).** `DEFINED → RESOLVED (versions pinned, hash known) → PROFILED (resource profile measured) →
RETIRED`; `FAILED` when resolving fails. A build in use by an active lease can't be retired.

**Host, new (D4).** `ONLINE ⇄ DRAINING` (no new leases, existing ones run out) `→ MAINTENANCE`; `OFFLINE` when
unreachable (reconcile).

### 4.3 Rules between entities (guards and cascades)

- A run can be RUNNING or PAUSED only while its lease is READY/CONNECTED; a lease going to DESTROYING or FAILED first
  ends its live runs (ABORTED, reason "lease ended …"). Today this is done ad hoc in the runs sync.
- A request can be ADMITTED only if its account is ACTIVE or WARNED and a host has a free slot for the family.
- A lease's charge is OPEN exactly while the lease is not DESTROYED.
- A build can't be RETIRED while any active lease uses it.

## 5. Operations: ls/ps, idle, placement

### 5.1 ls / ps (operator view)

| API | Shows |
|---|---|
| `GET /v1/leases?state=&account=&family=` | all leases: id, spec/build, owner, account, state, since, runs, sim-minutes so far |
| `GET /v1/runs?state=` | all runs: lease, recipe, state, started, attempt, fidelity |
| `GET /v1/hosts` | hosts: state, slots used/free per family |
| `GET /v1/accounts` and `/v1/accounts/{id}/usage` | budget, used, state |
| `GET /v1/{kind}/{id}/history` | the transition log of one entity |

The host CLI gets a `ps` verb (containers with lease label, CPU, RAM, uptime) for reconcile and the operator view.
Who may see what: open question O3.

### 5.2 Stop others' sims

Only the owner releases today (the API checks the user). v2 adds an **operator** role: may release, suspend or
preempt any lease with a reason. The reason is in the transition log and shown to the owner.

### 5.3 Idle timeout

A READY lease without a run for the idle limit is released (`IDLE_EXPIRED`). Default proposal: warning at 30 min,
release at 60 min (open O6).

### 5.4 Priority (D6)

Request classes: `dev` (may preempt), `case` (normal), `check` (preemptible). Preempt modes as in Slurm: SUSPEND
(docker pause), REQUEUE (rerun from start), CANCEL. Who pays for a spoilt run: open O1.

### 5.5 Placement by reservation (D4)

A lease reserves a slot on a host that runs its family, from the build's resource profile. "Lab full" = no slot
free, decided by the orchestrator. The host's CPU/RAM check (wrapper, exit 75) stays as a safety net only. That
fixes the lagging load-average refusals seen in SP6.

## 6. Cost: sim-minutes booked to the account

**Metric (decision 2):** sim time = lease wall time, in sim-minutes.

```
sim_minutes(lease) = (time of the lease's DESTROYED row − time of its REQUESTED row) / 60 s
```

Both times come from the transition log. Boot, idle, suspended and failed time all count: the slot was held.

- **Every request names an account** (`account_id`). Fab Suite sends the case's account; dev work uses a project
  account (e.g. `dev-mercury`); checks use `check`. A request without an account is refused (400) once D3 is on.
- **Accounts** have `kind` (case | project | check), `budget_minutes` (null = no limit), `warn_at` (e.g. 80 %).
- **Charge** = one row per lease (§4.2): `lease_id, account_id, unit = 'sim-minute', quantity, started_at,
  ended_at, state`. While OPEN, the quantity is computed live (now − start), so ls/ps shows cost so far.
- **Breakdown** (not charged): per run its wall time, plus the lease's boot time (REQUESTED → READY) and idle time
  (READY without a run). Answers "where did the minutes go".
- **Admission check:** a new lease on an EXHAUSTED account → REJECTED "budget used up". No hold/estimate yet:
  with one simple metric, "budget left > 0" is enough. Before start the API returns an **estimate** (spec's
  lifetime or the requester's planned minutes, plus boot time from the profile) so the requester sees what it will
  cost.
- **Export:** `GET /v1/accounts/{id}/charges?from=&to=` as JSON/CSV.

**Later extension, not built now** (the `unit` column leaves room): more units per lease (CPU-core-hours,
RAM-GB-hours, disk-GB-days for kept recordings, a start fee), charged as max(reserved, used) like OpenCost; a
versioned rate card with priority multipliers (preemptible cheaper, as spot pricing); EUR prices; export in FinOps
FOCUS columns (v1.4: ChargePeriodStart/End, ConsumedQuantity/Unit, BilledCost, EffectiveCost, BillingAccount,
Tags) for Shellback's accounting system.

## 7. Data model changes (sketch)

| Table | New | Notes |
|---|---|---|
| `transitions` | new | append-only, index `(entity_kind, entity_id, at)` |
| `requests` | new | spec, overlay hash, account, class, actor, state, lease_id |
| `accounts` | new | id, kind, name, budget_minutes, warn_at, state, case_id |
| `charges` | new | lease_id, account_id, unit, quantity, started/ended, state |
| `builds` | new (D2) | build_id (hash), spec, pinned versions JSON, profile JSON, state |
| `hosts` | new (D4) | id, families, slots, state |
| `instances` | + `account_id`, `build_id`, `request_id` | `state` written only by the engine |
| `runs` | + `attempt`, `fidelity`, states as §4.2 | host states mapped to engine events |

Old DBs are upgraded by `init_db` (ALTERs, as in M13). The per-provider DB trigger for the active limit stays until
D4's reservations replace it.

## 8. Steps (M14)

Docs first, tests and a local commit per step, push only on request.

| Step | What | Done when |
|---|---|---|
| **D0** | this doc + ARCHITECTURE §19 pointer + IMPLEMENTATION_PLAN M14 | user OK |
| **D1** | engine + `transitions` log; lease and run ported onto it, **no behaviour change** | all existing tests green; every legal edge tested, illegal ones raise; history API for one entity |
| **D2** | catalogue (families, hardware, software, plants, specs) + builds with hash; MERCSC0000 v1.2 = first build; FSI114–116 as hardware entries (whether their twins are runnable yet depends on mercury-reference) | lease records its build; same spec + overlay → same build id |
| **D3** | accounts + charges in sim-minutes; account required; hard stop on EXHAUSTED; estimate; export | a lease's charge = DESTROYED − REQUESTED from the log; refused when exhausted |
| **D4** | ls/ps API, host `ps` verb, hosts + reservations, operator role, idle timeout | operator sees all leases with cost so far; "lab full" by slots, not load |
| **D5** | run pause/resume/abort (lockstep), lease suspend/resume (docker pause) | WASHDOWN paused 1 min and resumed finishes, marked fidelity = paused |
| **D6** | request queue, classes, preemption | a dev request preempts a check; the check is requeued and reruns |
| **D7** | Fab Suite: case account, cost on the case, release reasons shown | a case shows its sim-minutes |

## 9. Open questions (my default in brackets)

- **O1** Who pays for a run spoilt by preemption? [its minutes go to a lab `overhead` account, not to the victim]
- **O2** Grow the state tables step by step, or the full model in D1? [D1 ports today's states only; new ones with
  their step]
- **O3** Who may see ls/ps of all users? [everyone sees all leases, without setup details; only operators act]
- **O4** Who is operator? [a config list of users]
- **O5** Keep "1 user = 1 active lease"? [replace it with a per-account limit of active leases]
- **O6** Idle timeout [warn at 30 min, release at 60 min]
- **O7** Is a lease folder's disk use (≈140 MB each, kept for recordings) part of the cost now? [no, later as
  disk-GB-days; pruning is a separate item]

## 10. Sources

- Slurm accounting and TRES billing: <https://slurm.schedmd.com/tres.html>
- OpenCost specification (max(request, usage), idle cost): <https://www.opencost.io/docs/specification>
- FinOps FOCUS specification v1.4: <https://focus.finops.org/focus-specification/>
- python-statemachine (compound states, persistence example): <https://github.com/fgmacedo/python-statemachine>
