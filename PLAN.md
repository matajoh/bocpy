# Draft PR plan — per-worker queues + work-stealing for `boc_worker`

**Format.** This branch lands as a single merged unit. The commits
below exist for `git bisect` and reviewer narrative, **not** as
shippable intermediate releases. Numeric perf targets are gated only on
the branch tip; intermediate commits are only required to build, run,
and pass the existing test suite.

**Verona-fidelity rule.** Each commit cites the Verona file/construct
it ports. Every deviation is called out with a one-paragraph
justification referencing the file it departs from. Memory orderings
are copied verbatim from `.copilot/verona-rt/src/rt/sched/mpmcq.h`; no
"optimised" relaxations.

**Default venv:** `.env314`. Free-threaded gate: `.env315t` runs the
full suite + the new scheduler tests on every commit from C2 onward.

---

## Branch overview

| Commit | Slug                      | Verona phase(s)            | Verona files (primary)                                                                                                                  |
|--------|---------------------------|----------------------------|-----------------------------------------------------------------------------------------------------------------------------------------|
| **C-1**| `tu-split`                | (mechanical pre-work)      | — (no Verona content; pure TU extraction)                                                                                               |
| **C0** | `sched-instr`             | Phase 0 (no behaviour)     | `schedulerstats.h`                                                                                                                       |
| **C1** | `sched-mpmcq`             | Queue primitive            | `work.h`, `mpmcq.h`                                                                                                                      |
| **C2** | `sched-perworker-handoff` | Phases 1+2 fused           | `core.h`, `schedulerthread.h` (`get_work`, `next_work`, `BATCH_SIZE`, `schedule_fifo`), `threadpool.h` (`round_robin`)                   |
| **C3** | `sched-stealing`          | Phase 3                    | `schedulerthread.h` (`try_steal`, `steal`), `core.h` (`token_work`, `should_steal_for_fairness`), `mpmcq.h` (`acquire_front`, `dequeue_all`) |
| **C4** | `sched-wsq-fanout`        | Phase 4 (gated)            | `workstealingqueue.h`, `ds/wrapindex.h`                                                                                                  |

C4 is **conditional**: it lands only if C3 stats show residual
single-queue producer contention on a fan-out workload. Otherwise it is
dropped from the branch.

Phase 5 (per-worker terminator delta) is explicitly **out of scope**
for this branch (per `00-context.md` §"Out of scope"); the structs
introduced here leave room for it without further refactoring.

---

## Disagreement resolutions (D1–D7)

Each decision below records the chosen option, the rebuttal argument
that drove it, and any concession from the losing lens.

### D1 — `pr-granularity`: 4 commits (instr, queue, handoff+affinity, steal)

**Choice.** Adopt usability's revised commit decomposition (B1→B2→B3
plus optional B4), refined to also split the queue primitive (C1) from
its first user (C2) per usability's PR-3 rebuttal table.

**Why.** The decisive arguments:

- *Usability rebuttal §1 (TSAN bisect surface).* The densest
  memory-model risk lives in `acquire_front` (steal). Fusing it with
  hand-off + affinity destroys the bisect signal. Speed's claim that
  intermediate states "livelock chain-ring" does not hold once the
  branch is read in order: in C2, all chain work stays affinity-pinned
  to one worker via producer-locality (§G1) → `pending` → local queue; idle
  workers park on their per-worker condvar. There is nothing to
  livelock on because the producer of the chain *is* the worker
  draining `pending`. Speed's "the producer is now waiting" scenario
  presumes round-robin dispatch without affinity, which we never
  ship — affinity is in the same commit (C2) as the per-worker queue.
- *Conservative rebuttal §1 (concession).* Conservative withdrew the
  PR-1-alone split; the only remaining boundary they defend is steal vs
  hand-off, which matches our C2/C3 cut.
- *Speed rebuttal §3 (concession kept).* Speed's instrumentation-first
  + multi-queue-last bookends are preserved (C0, C4). The middle is the
  only point of disagreement, and the bisect/TSAN argument is
  asymmetric: bundling forfeits diagnostic capability with no
  countervailing performance benefit (perf is only measured at the tip).

C1 (queue primitive in isolation, exercised by a C-level stress test
under TSAN) directly answers usability's stronger plan §3 argument that
`MPMCQ::acquire_front` deserves its own bisect anchor.

### D2 — `file-organisation`: introduce `src/bocpy/sched.{h,c}` from C0

**Choice.** Add the new translation unit. Place hot-path enqueue,
`pending`/batch take, and the worker-pop fast path as `static inline`
functions in `sched.h` so the inliner sees them at all call sites
inside `_core.c`.

**Why.**

- *Usability rebuttal §1+§3 (decisive).* `_core.c` is already 5905
  lines. The added scheduler is on the order of 1000 dense lines. A
  separate TU is the bare minimum partition for review, TSAN
  suppressions, and `git log -- src/bocpy/sched.c` bisect.
- *Conservative rebuttal §1 — addressed by `static inline` in the
  header.* Their strongest objection is loss of cross-TU inlining on
  the hot path. Putting the small hot functions (`boc_sched_dispatch`
  body, `pending` take, `boc_sched_worker_pop_fast`) `static inline` in
  `sched.h` lets the compiler fold them into `_core.c` callers without
  `-flto`. The cold paths (steal loop, MPMCQ enqueue/dequeue, park) live
  in `sched.c` and never benefit from inlining anyway.
- *Conservative rebuttal §1 — `setup.py` churn.* One-line change, paid
  once. Build matrix risk is bounded because all five venvs already
  build a multi-source extension (`_core.c` + `_math.c`).
- *Conservative rebuttal §4 (Verona is not the precedent).* Partially
  granted. We do not adopt the full Verona directory split; one
  `sched.{h,c}` pair is the minimum form of the same partition Verona
  applies. No exported symbols leak past `_core.c` — `sched.h` is
  `#include`d only by `_core.c`.

### D3 — `mpsc-vs-mpmc-first`: port full `MPMCQ` verbatim in C1

**Choice.** C1 lands the complete `MPMCQ<BOCBehavior *>` port —
`enqueue`, `enqueue_segment`, `dequeue`, `dequeue_all`, `acquire_front`,
`is_empty` — with memory orderings identical to
[`mpmcq.h`](../../verona-rt/src/rt/sched/mpmcq.h).

**Why.** Both usability and conservative explicitly conceded:

- Conservative §rebuttal: "MPSC code shape with MPMCQ orderings is
  literally typing the same code twice with the same atomics, then
  deleting a comment in PR-3." Concession adopted.
- Usability §1: "MPSC-first costs a full rewrite of `mpmcq.{h,c}`
  between the bisect intervals where stealing is absent vs. present,
  and the *final* reviewer never sees the MPSC code at all."
  Concession adopted.
- Usability §3 staging is preserved: the *callers* are staged across
  C2 and C3 so each commit exercises a strictly larger subset of the
  queue's API. The queue itself is written once.

C1 includes a C-level TSAN stress test (driven from pytest) that
hammers `enqueue` from N producer threads against `dequeue_all` +
`acquire_front` from a second consumer — exactly the steal race — so
the dense surface gets its own bisect anchor before any production code
calls it.

### D4 — `shutdown-channel`: out-of-band `stop_requested` flag + condvar broadcast

**Choice.** Each `BOCWorker` carries an `_Atomic(bool) stop_requested`.
`stop_workers` sets it on every worker, then broadcasts each per-worker
condvar. `worker_dequeue` checks the flag only on the park path, under
the same mutex guarding the condvar wait. **`terminator_count` is
never consulted on the worker exit path** — quiescence is transient,
shutdown is durable; conflating them breaks the `start()`/`wait()`/
`start()` re-entry pattern (see G7 incarnation counter) and races
`stop_workers` against the cleanup-message phase. `wait()` remains the
sole observer of `terminator_count`. No sentinel `BOCBehavior` in the
queue; no `receive()` in the steady-state worker loop.

**Why.** All three lenses agreed on removing `receive()` from the hot
path. The remaining disagreement was sentinel (speed/conservative-final)
vs out-of-band flag (usability):

- *Usability rebuttal §4 — Verona precedent (decisive).* Verona's
  shutdown is exactly an out-of-band flag plus an unpause broadcast
  (`.copilot/verona-rt/src/rt/sched/threadpool.h:335-340`: `forall(...
  thread->stop()); h.unpause_all();`). The "do what Verona does"
  default in `00-context.md` lands on usability here. Speed's sentinel
  is a deviation that Verona itself rejected.
- *Speed rebuttal §1 — hot-path cost.* Equally satisfied by the flag
  approach: the flag is read **only** on the park path under the
  mutex, never by the fast `pending`/queue dequeue. Speed's "zero
  cycles per pop" property is preserved.
- *Conservative rebuttal §3 — Verona precedent.* Conservative also
  cites threadpool.h:335-340. They mis-attributed it as supporting the
  sentinel; the actual code supports the flag. We follow the code.
- *Usability rebuttal §3.* The exact protocol they sketch — flag
  check + queue peek + `pthread_cond_wait`, all under one mutex — is
  also the re-check protocol required by analysis Gap #2
  (terminator-park race). One mechanism handles both.

**Deviation note vs Verona.** Verona uses one global `ThreadSync`
condvar; we use a per-worker condvar (already the bocpy precedent in
the existing message-queue waiter logic). Justification: each producer
already targets exactly one worker, so a per-worker `cond_signal`
strictly dominates a global broadcast on the new-work path.
Cross-worker shutdown remains a broadcast loop. Cited deviation from
[`threadsync.h`](../../verona-rt/src/rt/sched/threadsync.h).

### D5 — `token-representation`: real sentinel `BOCBehavior` with `is_token` field

**Choice.** Each `BOCWorker` owns a `BOCBehavior *token_work`
allocated at worker init, freed at shutdown. The struct gains a
`uint8_t is_token` flag (placed in existing padding, no struct growth).
`worker_dequeue` checks `is_token` exactly once after dequeue.

**Why.** Usability conceded in their rebuttal §"Concession": "The
tag-bit was a deviation I could not justify under the Verona-fidelity
rule." Speed's rebuttal §1+§2 (zero hot-path cost vs three-site mask
discipline; cite `core.h:30-37` and `core.h:46-51` for construction
and destruction) is the agreed mapping.

### D6 — `batch-size-tunability`: hardcoded `static const size_t BATCH_SIZE = 100`

**Choice.** No env var. Constant defined in `sched.h` next to the
Verona citation.

**Why.** Conservative conceded in their rebuttal: "An env var is *new
public API surface*, and the conservative lens exists to minimise
exactly that. … Match Verona. Hardcode 100." Speed's argument about
constant-folding the `if (--batch == 0)` decrement is preserved as a
secondary benefit. Cite
[`schedulerthread.h:129`](../../verona-rt/src/rt/sched/schedulerthread.h#L129).

### D7 — `round-robin-cursor`: per-thread TLS cursor with incarnation re-seed

**Choice.** Two `_Thread_local` words in `sched.c`:
`static _Thread_local size_t rr_incarnation;` and
`static _Thread_local BOCWorker *rr_nonlocal;`. Re-seed when the
runtime's incarnation counter changes (incremented by `start()` /
`stop()` cycles). Walk the worker ring on each use.

**Why.** This is the one disagreement where the rebuttals reveal a
factual error in usability's plan: usability's rebuttal §3 claims
"Verona uses a single relaxed atomic counter." Verona does not. The
actual code at
[`threadpool.h:147-167`](../../verona-rt/src/rt/sched/threadpool.h#L147)
is exactly the TLS+incarnation pattern speed proposes (verified:
`static thread_local size_t incarnation; static thread_local Core*
nonlocal; if (incarnation != get().incarnation) { ... } else nonlocal
= nonlocal->next;`). Under the Verona-fidelity rule the global atomic
is the unjustified deviation, not the TLS. Speed also concedes that
their original "periodic flush" flourish was overengineering — the
cursor *is* local state by construction; nothing is flushed.

The runtime incarnation counter already needs to exist for shutdown /
restart bookkeeping (see Gap #3 below); the TLS cursor reuses it.

---

## Gap closures (analysis §"Gaps none of the planners addressed")

### G1 — Behavior-dispatch site (producer-locality, not cown affinity)

**Naming.** The plan previously called this "cown affinity". That term
over-claims: bocpy has **no per-cown home worker**. The locality bias
is purely the transient producer's identity at the moment it calls
`boc_sched_dispatch`. Renamed throughout to **producer-locality**
(matches Verona's `schedule_fifo` framing exactly,
[`schedulerthread.h:86-101`](../../verona-rt/src/rt/sched/schedulerthread.h#L86)).
Introducing per-cown affinity is **out of scope** for this branch.

**Single dispatch point, four callers.** `behavior_resolve_one` at
line ~4494 is the **only** function that publishes a behaviour to a
worker queue. The swap of `boc_enqueue(start_message)` →
`boc_sched_dispatch(behavior)` is **internal** to that function.
Its callers in current `_core.c` are:

| Line | Caller | Thread | GIL | Producer-locality branch | Steady-state PL fraction (chain-ring W=8) |
|------|--------|--------|-----|--------------------------|-------------------------------------------|
| ~4646 | `BehaviorCapsule_schedule` (dedup) | caller (main or worker) | held | depends on TLS `current_worker` | small (user-thread entry) |
| ~4812 | `BehaviorCapsule_schedule` (end) | caller (main or worker) | held | depends on TLS `current_worker` | small (user-thread entry) |
| ~5200 | `request_release_inner` | worker | held | producer-locality (worker's own `pending`/queue) | 1.0 (always worker) |
| ~5227 | `request_start_enqueue_inner` no-predecessor fast path | caller (main or worker) | **released** (`Py_BEGIN_ALLOW_THREADS` in caller) | depends on TLS `current_worker` | ≈ 1.0 on chain-ring (running behaviour schedules its successor) |

On the perf-gate workload (chain-ring), lines ~5200 and ~5227
dominate, both routing through the producer-locality branch. The
§G2 protocol must be correct **for the case where every producer
is producer-local**, because that is the hot case.

The line-5227 path is the most performance-sensitive of the four
(link-loop hot path, GIL released). `_Thread_local` is GIL-independent,
so the TLS load of `current_worker` from inside `boc_sched_dispatch` is
well-defined here. The slow arm of `boc_sched_dispatch` takes a
`pthread_mutex_t` (`cv_mu`); pthread mutexes are unaffected by the
GIL state.

`boc_sched_dispatch` reads `_Thread_local BOCWorker *current_worker`
(set on entry to `worker_dequeue`, cleared on exit, `NULL` for non-
worker producers) to pick between the producer-locality and round-
robin paths. One TLS load + one branch — no `pthread_self()`, no
global lookup.

The C2 commit description lists all four line numbers.

### G2 — Park / unpark protocol (two-epoch `pause`/`unpause` port)

**The race.** Per-worker condvars do not by themselves close the
producer-on-other-worker liveness gap. A worker that publishes to its
own producer-local `pending`/queue (the dominant case under producer-
locality, §G1) does not signal anyone else. A peer worker that began
parking an instant before the publish would sleep until shutdown.

**Decision: port Verona's two-epoch `pause`/`unpause` protocol
verbatim from
[`threadpool.h:282-379`](../../verona-rt/src/rt/sched/threadpool.h#L282)
as a non-optional part of C2.** A single counter plus a sampled-but-
never-compared epoch is *not* equivalent and reproduces the race in a
different shape. Verona's design uses two `_Atomic(uint64_t)` epochs
(`pause_epoch`, `unpause_epoch`) and a CAS on the producer side that
is the linearisation point ordering "I published" against "you
parked".

Fields in `sched.c` (mirroring Verona):

```
static _Atomic(uint64_t) pause_epoch   = 0;  /* bumped by parker */
static _Atomic(uint64_t) unpause_epoch = 0;  /* CAS'd by producer */
static _Atomic(uint32_t) parked_count  = 0;  /* fast-path skip */
```

Per-worker `boc_sched_worker_t` gains `_Atomic(bool) parked`
(explicitly atomic; default-initialised to `false`).

**Producer (slow arm of `boc_sched_dispatch`, after the publish):**

```
uint64_t pe = atomic_load_explicit(&pause_epoch, memory_order_acquire);
uint64_t ue = atomic_load_explicit(&unpause_epoch, memory_order_acquire);
if (pe == ue) {
    /* No parker has bumped pause_epoch since the last unpause.
       The fast common case: nobody is racing a park; bail. */
    if (target != current_worker)
        boc_sched_signal_one(target);   /* targeted cross-worker wake */
    return;
}
/* Try to claim responsibility for the wake by catching unpause_epoch
   up to pause_epoch. The CAS is the linearisation point. */
if (atomic_compare_exchange_strong_explicit(
        &unpause_epoch, &ue, pe,
        memory_order_acq_rel, memory_order_acquire)) {
    boc_sched_unpause_one();   /* walk ring, signal one parked peer */
}
if (target != current_worker) boc_sched_signal_one(target);
```

**Parker (under `cv_mu`):** the parker bumps `pause_epoch` *before*
any re-check, then walks **every** worker's public queue
(`check_for_work` analogue,
[`threadpool.h::check_for_work`](../../verona-rt/src/rt/sched/threadpool.h)),
then samples `unpause_epoch`, locks `cv_mu`, re-compares, and only
sleeps if the sampled `unpause_epoch` still equals the live one.
**`stop_requested` is checked at the top of the loop — before any
`pause_epoch` bump — so a worker exiting on shutdown does not advance
`pause_epoch` past `unpause_epoch` (preserves the §G7 shutdown
invariant).**

```
loop:
    if (atomic_load(&self->stop_requested)) return NULL;
    if pending:                  return take_pending()
    if (w = boc_bq_dequeue(&self->q)): return w
    if (w = try_steal(self)):    return w                /* C3+ */

    /* Park-attempt: bump pause_epoch first so any concurrent producer
       sees pe != ue and is forced into the CAS arm. */
    atomic_fetch_add_explicit(&pause_epoch, 1, memory_order_seq_cst);
    uint64_t ue_snap = atomic_load_explicit(&unpause_epoch,
                                            memory_order_acquire);

    /* check_for_work: in C3+ walks ALL workers (cheap; bounded by
       worker_count is_empty reads). In C2 (no try_steal) walks only
       self->q — a peer cannot act on visible peer work, so the broader
       walk would convert every cross-worker publish into a busy-spin
       on every parked peer. The C3 commit widens this to
       `boc_sched_any_work_visible()`. */
#if BOC_HAVE_TRY_STEAL   /* turned on in C3 */
    if (boc_sched_any_work_visible()) goto loop;
#else
    if (boc_bq_peek(&self->q))        goto loop;
#endif

    pthread_mutex_lock(&self->cv_mu);
    if (self->stop_requested)        { unlock; return NULL; }
    /* Final epoch re-check under cv_mu: if a producer caught up,
       skip the wait. terminator_count is NOT consulted here —
       quiescence is transient; only stop_requested causes exit. */
    if (atomic_load_explicit(&unpause_epoch, memory_order_acquire)
        != ue_snap) { unlock; goto loop; }

    atomic_store_explicit(&self->parked, true, memory_order_release);
    atomic_fetch_add_explicit(&parked_count, 1, memory_order_acq_rel);
    pthread_cond_wait(&self->cv, &self->cv_mu);
    atomic_fetch_sub_explicit(&parked_count, 1, memory_order_acq_rel);
    atomic_store_explicit(&self->parked, false, memory_order_release);
    pthread_mutex_unlock(&self->cv_mu);
    goto loop;
```

**Why this closes the race.** The parker's `pause_epoch` increment
is `seq_cst` and happens *before* the `check_for_work` walk and the
`cv_mu` re-check of `unpause_epoch`. A producer publishing concurrently
will (a) make its work visible to `check_for_work`, and (b) load
`pause_epoch > unpause_epoch` and enter the CAS arm. Whichever side
wins the CAS issues the signal; the other side either bails out at the
epoch re-check under `cv_mu` (parker) or has already done the work
(producer). This is exactly Verona's argument at
[`threadpool.h:282-379`](../../verona-rt/src/rt/sched/threadpool.h#L282).

**`boc_sched_unpause_one(void)`:** walk the worker ring once from a
TLS rotating cursor; for each worker, load `parked` with acquire
ordering; for the first worker observed `parked == true`, lock its
`cv_mu` and `cond_signal`. Bounded to one signal per CAS-winner; if
the walk finds nobody (because the parker is between epoch re-check
and `parked` store), the next producer that enters the CAS arm picks
up the work — spurious-wake-with-no-target is rare because the CAS
itself only fires when there *was* a `pause_epoch` bump.

**`stop_workers`:** sets `stop_requested` on every worker, then
broadcasts every per-worker `cv` (D4); the unpause path is bypassed
because `stop_requested` is checked first under `cv_mu`.

**Documented deviations from Verona** (per the Verona-fidelity rule;
these deviations are intentional and called out here, not buried):

1. **Per-worker condvars** instead of one global `ThreadSync` condvar
   ([`threadsync.h`](../../verona-rt/src/rt/sched/threadsync.h)).
   Justification: the targeted cross-worker `cond_signal` arm wins
   on every steady-state new-work wake (one signal, one wake). The
   `boc_sched_unpause_one` arm pays a ring walk only on the parker-
   producer race window (gated by the CAS), which is rare. bocpy
   precedent: existing `BOCQueue` waiters are per-queue.
2. **`boc_sched_signal_one(target)` lock-then-signal** on the
   targeted arm instead of Verona's global `sync.handle`
   acquisition. Same rationale.
3. **No `state.get_active_threads()`** — bocpy has `parked_count`
   instead, used only as a fast-path skip on the producer side
   (fast arm: if `parked_count == 0` and `pe == ue`, skip the CAS
   load entirely). Behavioural equivalence is preserved because the
   epoch comparison alone is sufficient; `parked_count` is an
   optimisation, not a correctness primitive.

The protocol is implemented in C2 (when parking is first introduced)
and is unchanged in C3 except for widening `check_for_work` from
self-only to all-workers (gated on `BOC_HAVE_TRY_STEAL`).

**Combined Verona-fidelity audit.** Read together, bocpy's park/unpark
protocol substitutes:

| Verona | bocpy | Why the substitution preserves the protocol |
|--------|-------|----------------------------------------------|
| One global `ThreadSync` condvar | Per-worker `cv_mu` + `cv` | Targeted wake on the common case (one publish → one signal); ring-walk fallback when the parker-producer race window is open |
| `sync.handle` for the pause section | Per-worker `cv_mu` | Same property: serialises a parker's epoch re-check against a producer's wake under the same mutex |
| `state.get_active_threads()` | `parked_count` (fast-path skip only) + `parked` flag (correctness) | The CAS on `unpause_epoch` is the linearisation point either way; `parked_count` is the optimisation that lets `pe == ue` short-circuit |
| `unpause_slow` ring walk over cores | `boc_sched_unpause_one` ring walk over workers | Identical shape — walk the ring, signal the first parker found |

The primitives are different at the surface; the protocol — two
epochs, parker bumps `pause_epoch` `seq_cst` before re-check,
producer CASes `unpause_epoch` forward as the linearisation point,
winner of the CAS issues the signal — is identical.

### G3 — Worker-death / index-reuse policy

There is no worker-death code today and none is added. The branch
documents and asserts:

- `worker_count` is captured at `start()` and is **immutable** until
  `stop()`. `BOCWorker workers[worker_count]` is allocated once.
- Per-worker `BOCWorker.owner_interp_id` is set on first
  `boc_sched_worker_register(my_index)` and never changes for the
  lifetime of the runtime instance.
- `boc_sched_worker_register` raises `RuntimeError` if the index is
  out of range or already claimed by a different interpreter.
- A future worker-death feature must reconsider both the array
  immutability and the TLS round-robin incarnation re-seed (G7).
  This is recorded as a `// TODO(worker-death):` block comment at the
  top of `sched.c`.

### G4 — Cleanup-channel coupling (`worker.py::cleanup`)

Workers continue to use `receive()` for **all** non-behaviour traffic:
the `boc_cleanup` tag in `worker.py::cleanup`, plus any other tags they
already poll (`boc_behavior` startup ack, etc.). Only the `boc_worker`
*behaviour* path moves to `boc_sched_worker_pop`.

C2's edit to `worker.py::do_work` is therefore narrowly scoped:

```python
_core.boc_sched_worker_register(self.index)
send("boc_behavior", "started")
while True:
    behavior = _core.boc_sched_worker_pop()  # blocks; returns None on stop
    if behavior is None:
        break
    run_behavior(behavior)
# cleanup() below this point is unchanged; still uses receive("boc_cleanup")
```

The `BOCQueue` infrastructure remains for shutdown ack, `boc_cleanup`,
`boc_noticeboard`, `snap`, and tests — explicitly per agreement #11.

### G5 — `__init__.pyi` stub cadence

Every commit that touches the `_core` module surface updates
`src/bocpy/__init__.pyi` in the same commit. The cadence is:

- C0: add `_core.scheduler_stats` stub.
- C2: add `_core.boc_sched_worker_register` and
  `_core.boc_sched_worker_pop` stubs (both marked private with leading
  underscore convention; not re-exported from `bocpy.__init__`).
- C3: extend `scheduler_stats` stub docstring with new counters
  (`popped_via_steal`, `steal_attempts`, `steal_failures`, `parked`,
  `last_steal_attempt_ns`).
- C4 (if landed): nest `subqueues: list[dict]` in the per-worker stats
  dict — top-level keys unchanged.

Stub docstrings follow the Sphinx style required by the
`commenting-c-and-python` skill.

### G6 — TLS model

**Decision: default TLS model.** Do **not** use
`__attribute__((tls_model("initial-exec")))` on any of the new
`_Thread_local` variables (`pending`, `batch`, `current_worker`,
`rr_incarnation`, `rr_nonlocal`).

Justification: `initial-exec` is faster but requires the loader to
allocate the TLS slot in the static block at executable startup. A
CPython extension module is `dlopen`'d after the interpreter is
running; under sub-interpreter and free-threaded loading paths
(`.env313t`, `.env315t`) `initial-exec` TLS is at risk of
`dlopen` failure or inconsistent semantics across interpreters. The
default model (`global-dynamic`) costs one extra `__tls_get_addr` call
per access on the cold paths, which is acceptable because the hot
paths (`pending`, `batch`) are accessed at most twice per
behaviour-dispatched-locally and the cost is dwarfed by the work the
behaviour itself does.

If perf measurement on `.env315t` later shows `__tls_get_addr` as a
real bottleneck, revisit by using a single TLS struct pointer (one TLS
lookup, multi-field struct read) rather than promoting to
`initial-exec`.

### G7 — Runtime incarnation counter

Required by D7 (TLS round-robin cursor invalidation across `start()`/
`stop()` cycles) and by G3 (worker-array immutability invariant).

`sched.c` declares `static size_t boc_sched_incarnation = 0;` (plain
`size_t`, not `_Atomic` — Verona uses a plain `size_t`, see
[`threadpool.h:40`](../../verona-rt/src/rt/sched/threadpool.h#L40)).

**Cadence.** `boc_sched_init` is called from `behaviors.start()` on
**every** start cycle (not once at module init). Each call:

1. Allocates a fresh `BOCWorker workers[worker_count]` array (G3's
   "immutable until `stop()`" rule applies *within* a cycle; the
   array is freed and reallocated *across* cycles).
2. Increments `boc_sched_incarnation` *under the same
   `start()`/`stop()` lock* that serialises lifecycle transitions.
3. Spawns the workers; the increment establishes a happens-before
   edge to every worker's first TLS read.

`boc_sched_shutdown` is called from `behaviors.stop()`, frees the
`workers[]` array, and asserts:

- `parked_count == 0` — ⇒ no worker is sleeping in `cond_wait`.
- `pause_epoch >= unpause_epoch` — ⇒ every issued `pause_epoch` bump
  has either been claimed by a producer CAS or is from a worker that
  has since exited via the top-of-loop `stop_requested` check (which
  fires *before* the bump). The reverse inequality would indicate a
  producer-side overshoot bug.

TLS-side reads are plain loads. Reseed pattern mirrors
[`threadpool.h:162-165`](../../verona-rt/src/rt/sched/threadpool.h#L162).

The C2 re-entry test runs ***three*** `start()`/`wait()` cycles to
catch off-by-one (e.g. an `init` that increments only on the first
call).

### G8 — Cross-interpreter dispatch ownership

C2 removes the `BehaviorCapsuleObject`+`BOCMessage` wrapping that
currently ferries `BOCBehavior *` across the sub-interpreter boundary.
The new path publishes raw `BOCBehavior *` into the per-worker
`boc_bq_t`. The ownership rules below preserve constraint #2 from
[`00-context.md`](00-context.md) **and** constraint #3 (link-loop
infallibility) by eliminating the consumer-side allocation entirely
and by pairing every successful pop with a guaranteed release call:

1. **Producer side (allocation-free, infallible):** before calling
   `boc_sched_dispatch`, the producer holds a strong reference to the
   `BOCBehavior` via `BEHAVIOR_INCREF`. `boc_sched_dispatch` takes
   ownership of that reference; on return it has been logically
   transferred to the consumer via the queue.
2. **Consumer side (infallible by pre-allocation):** each worker owns
   one **pre-allocated, per-worker `PyCapsule`** allocated in the
   worker's interpreter at `boc_sched_worker_register` time and freed
   at worker shutdown. `boc_sched_worker_pop` pops the raw
   `BOCBehavior *`, calls `PyCapsule_SetPointer` on the pre-allocated
   capsule (allocation-free, infallible), and returns the capsule.
3. **Paired release — mandatory `try/finally` contract.** Every
   successful `boc_sched_worker_pop` must be paired with exactly one
   `_core.boc_sched_worker_release(behavior)` call, regardless of
   whether `run_behavior` returns normally or raises.
   `boc_sched_worker_release` runs the C-side cleanup that today
   lives in the capsule destructor: `behavior_release_all` over the
   cowns, `BEHAVIOR_DECREF` on the popped behaviour, and
   `terminator_dec`. The `worker.py::do_work` loop is
   structured as:

   ```python
   while True:
       behavior = _core.boc_sched_worker_pop()
       if behavior is None:
           break
       try:
           run_behavior(behavior)
       finally:
           _core.boc_sched_worker_release(behavior)
   ```

   The `finally` is the safety net that today's `tp_dealloc` provides
   implicitly. Any uncaught exception from `run_behavior`
   (`MemoryError`, `KeyboardInterrupt`, `SystemExit` during
   sub-interpreter teardown, or any future regression) still runs
   `release` and leaves the runtime in a state where `wait()` can
   complete.
4. **Capsule destructor.** The recycled per-worker capsule has a
   destructor that asserts the wrapped pointer is `NULL` (i.e.
   `boc_sched_worker_release` cleared it). This catches a bug where
   `worker.py` skips the `finally` clause (the assertion fires at
   sub-interpreter teardown, surfacing the leak loudly rather than
   silently).
5. **Free-threaded build (`.env315t`):** `PyCapsule_SetPointer` and
   the refcount ops above are documented atomic-safe on free-threaded
   CPython; the C2 cross-cutting checklist exercises this path under
   TSAN on `.env315t`.
6. **Constraint #3 audit:** with consumer-side allocation eliminated
   and the paired-release `finally` guaranteed by the worker loop
   shape, the only remaining failure mode in dispatch is the
   producer-side `BEHAVIOR_INCREF` (refcount op, no allocation, no
   failure mode). The link loop remains infallible end-to-end.
7. **Test:** the C2 test suite includes a behaviour body that raises
   an uncaught `RuntimeError` from inside `@when` (bypassing
   `Cown.exception`), and asserts that `wait()` returns, the cown is
   re-acquirable by a follow-on behaviour, and the per-worker
   capsule's pointer-is-NULL assertion holds at teardown.

---

## Commits

### C-1 — `tu-split`: pure mechanical extraction of self-contained subsystems

**Scope.** Zero behaviour change. Move six self-contained subsystems
out of the 5905-line `_core.c` (and the duplicated polyfills in
`_math.c`) into their own translation units, before any scheduler
work begins. Lands as the **first** commit on the branch so every
subsequent commit's `git log -- <tu>` and `git blame` are scoped to
the subsystem actually being changed, and TSAN suppressions can be
file-scoped from C1 onward.

This commit ships strictly less code than today's two TUs combined
(deduplicated polyfills) and exactly the same symbols at link time.

**New translation units (all six headers `#include`d only by
`_core.c` and, where noted, `_math.c`):**

| New TU | Extracted from | Approx. lines | Notes |
|--------|----------------|---------------|-------|
| `compat.{h,c}` | `_core.c` (~1–460), `_math.c` (~1–110) | ~660 (single copy, post-extension) | MSVC `atomic_*` polyfill, `atomic_intptr_t` siblings, `BOCMutex`/`BOCCond` SRWLock-vs-pthread wrappers, `thrd_sleep`, `PyErr_GetRaisedException` polyfill. **Removes ≈110 lines of duplicated boilerplate from `_math.c`.** **Extended in this commit** to cover the full C11 atomics surface that C1/C2 need (typed `_Atomic(T)` wrappers and ordering-correct `*_explicit` macros on ARM64 Windows) — see "MSVC atomics extension" below. Header is `#include`d by both `_core.c` and `_math.c`. |
| `xidata.{h,c}` | `_core.c` (~3247–3350), `_math.c` (~60–110) | ~250 (single copy) | The `XIDATA_T` / `XIDATA_INIT` / `XIDATA_REGISTERCLASS` macro family that varies across `.env312`/`.env313t`/`.env314`/`.env315`/`.env315t`, plus `xidata_init` shim, `_new_contents_object`, `_contents_shared_free`, `_contents_shared`. **Removes the `_PyXIData_*` vs `_PyCrossInterpreterData_*` `#if PY_VERSION_HEX` ladder from both TUs.** Header is `#include`d by both `_core.c` and `_math.c`. |
| `noticeboard.{h,c}` | `_core.c` (~561–660 + ~1153–2017) | ~510 | `Noticeboard NB`, `NoticeboardEntry[]`, version counter, TLS snapshot cache, all `notice_*` C functions and Python wrappers. The one forward-reference to `CownCapsule` (for the cown-pin helper) is broken by an opaque `boc_cown_handle_t` typedef in `noticeboard.h` plus one `static inline` accessor exported from `_core.c`. |
| `tags.{h,c}` | `_core.c` (~530–920) | ~390 | `BOCTag` struct + `tag_from_PyUnicode` / `tag_to_PyUnicode` / `tag_decref` / `tag_incref` / `tag_disable` / `tag_compare_*` / `BOCTag_free`. The `BOCQueue::tag` field's incomplete-type usage is solved by including `tags.h` from `message_queue.h` (below). |
| `terminator.{h,c}` | `_core.c` (~698–740 + ~2018–2090) | ~150 | C-level `terminator_count` run-down counter. Pre-extracted because the out-of-scope Phase 5 per-worker terminator delta (§00-context.md) lands in this file later; getting it out of `_core.c` now eliminates one rename in that future PR. |
| `message_queue.{h,c}` | `_core.c` (~460–560 + ~3500–3700) | ~310 | `BOCQueue`, `BOCRecycleQueue`, `boc_enqueue`, `boc_dequeue`. **Lands here, in C-1, *not* in C0**, so that C0's added contention counters (`enqueue_cas_retries`, `pushed_total`, etc.) live in their final home from the moment they exist — no "add field then move file" double diff. |

**Files touched:**

- `setup.py` — add the six new `.c` files to `sources` for the
  `_core` extension; add `compat.c` and `xidata.c` to `_math`'s
  `sources` (replacing the duplicated inline copies). One-line edit
  per source list.
- `src/bocpy/_core.c` — deletes ≈3800 lines of struct/function bodies
  that move into the new TUs; gains ~6 `#include` lines. New size:
  ≈2100 lines. **No symbol changes** — every previously-`static`
  symbol that survives the move stays `static` (within its new TU);
  every previously-non-`static` symbol stays exported with the same
  signature.
- `src/bocpy/_math.c` — deletes ≈110 lines of `atomic_*` polyfill
  and ≈ 50 lines of `XIDATA_*` macro ladder; gains 2 `#include` lines.
- `src/bocpy/compat.h`, `src/bocpy/xidata.h`, `src/bocpy/noticeboard.h`,
  `src/bocpy/tags.h`, `src/bocpy/terminator.h`,
  `src/bocpy/message_queue.h` (new) — declarations + Sphinx-style
  doc-comments per `commenting-c-and-python`.
- `src/bocpy/compat.c`, `src/bocpy/xidata.c`, `src/bocpy/noticeboard.c`,
  `src/bocpy/tags.c`, `src/bocpy/terminator.c`,
  `src/bocpy/message_queue.c` (new) — implementations, byte-for-byte
  identical to what was extracted (modulo the moved
  `static`-vs-export decisions noted above).
- `src/bocpy/__init__.pyi` — unchanged. C-1 makes no Python-surface
  edits.
- `test/` — unchanged. C-1 ships no new tests; the existing suite
  *is* the regression harness for a pure refactor.

**Mechanical-extraction discipline (enforced in the commit message
and reviewed line-by-line):**

1. **No reformatting.** Lines move, they do not change. `clang-format`
   is not run. `git diff -M -C --find-copies-harder` should report
   each new TU's body as a copy of contiguous regions of `_core.c` /
   `_math.c`.
2. **No new `static`/non-`static` flips except where required.** A
   symbol that was `static` and is now called across TU boundaries
   gets a non-`static` declaration in the corresponding header and
   loses the `static` keyword in its definition; every such case is
   listed in the commit message. (Expected list: a handful of
   noticeboard helpers and at most two from `tags`.)
3. **No `#include` cycles.** The header dependency order is
   `compat.h` < `xidata.h` < `tags.h` < `terminator.h` <
   `message_queue.h` < `noticeboard.h` (which gets the opaque cown
   handle from `_core.c`).
4. **Forward-reference resolution for noticeboard.** The current
   `CownCapsule` forward declaration at `_core.c:578` is replaced
   by an opaque `typedef struct boc_cown_handle boc_cown_handle_t;`
   in `noticeboard.h`. `_core.c` defines
   `static inline boc_cown_handle_t *boc_cown_to_handle(CownCapsule *)`
   and the noticeboard's pin helper takes the opaque handle. Zero
   behaviour change; one indirection at the call site that the
   compiler folds.

**MSVC atomics extension (the one non-mechanical part of C-1).**

The existing polyfill in `_core.c` lines ~11–93 covers only
`int_least64_t` and `intptr_t`, only the implicit-barrier
`Interlocked*` family, and explicitly comments that
`memory_order_*` arguments are *"accepted but ignored"*. C1's
`mpmcq.h` port and C2's §G2 two-epoch CAS protocol both rely on
real acquire/release/acq_rel/seq_cst semantics on `_Atomic(uint64_t)`
and `_Atomic(bool)`, which today's polyfill cannot express — and on
ARM64 Windows (a supported target) the implicit-x86-TSO assumption
that makes the current code accidentally correct does not hold.

**Choice.** Extend the bocpy polyfill rather than require
`<stdatomic.h>` (Option A). Rationale: `<stdatomic.h>` on MSVC ships
only with VS 2022 17.5+ (`/std:c11 /experimental:c11atomics`,
Mar 2023) and unflagged with 17.8+ (Nov 2023). bocpy's MSVC floor
is VS 2019, which is required to support source builds against the
full set of CPython versions (3.10–3.15) without forcing users to
upgrade their toolchain. The TU split makes the extra ~200
polyfill lines a single-file maintenance burden, scoped to
`compat.{h,c}` and reviewed once.

**Surface added to `compat.h`** (POSIX path stays as today — alias
to `<stdatomic.h>` via macros so user code is platform-uniform):

```c
/* Typed atomic wrappers used by C1/C2.  POSIX: passthrough to
   `_Atomic(T)`.  MSVC: `volatile T` underneath, distinct typedef
   per width so the right Interlocked* dispatch is picked.  */
typedef volatile uint64_t  boc_atomic_u64_t;
typedef volatile uint32_t  boc_atomic_u32_t;
typedef volatile uint8_t   boc_atomic_bool_t;   /* sizeof(bool) == 1 */
typedef volatile void *    boc_atomic_ptr_t;    /* generic pointer slot */

/* Memory-order tags.  Distinct integer constants on MSVC so the
   `boc_atomic_*_explicit` family can dispatch via a `switch` (or,
   in the inline-fast path, via `_Generic` + `__builtin_choose_expr`
   on gcc/clang and a constexpr-style switch on MSVC). */
typedef enum {
    BOC_MO_RELAXED = 0,
    BOC_MO_ACQUIRE = 2,   /* skip 1 to leave room for `consume` */
    BOC_MO_RELEASE = 3,
    BOC_MO_ACQ_REL = 4,
    BOC_MO_SEQ_CST = 5,
} boc_memory_order_t;
```

**MSVC dispatch table (the core of the extension).** Each ordering
maps to one ARM64-correct Interlocked* variant; on x86/x64 every
variant is a full barrier, so the table also satisfies stronger-
than-requested orderings on those platforms.

| `boc_memory_order_t` | `Interlocked*` suffix on ARM64 | x86/x64 effect |
|---|---|---|
| `BOC_MO_RELAXED` | `*NoFence`           | full barrier (free) |
| `BOC_MO_ACQUIRE` | `*Acquire`           | full barrier (free) |
| `BOC_MO_RELEASE` | `*Release`           | full barrier (free) |
| `BOC_MO_ACQ_REL` | `*` (full)           | full barrier |
| `BOC_MO_SEQ_CST` | `*` (full)           | full barrier |

The operations exposed (one entry per (op, type) pair, hand-written
on MSVC, macro-aliased on POSIX):

- `boc_atomic_load_<T>_explicit(ptr, order)`
- `boc_atomic_store_<T>_explicit(ptr, val, order)`
- `boc_atomic_fetch_add_<T>_explicit(ptr, val, order)`
- `boc_atomic_fetch_sub_<T>_explicit(ptr, val, order)`
- `boc_atomic_exchange_<T>_explicit(ptr, val, order)`
- `boc_atomic_compare_exchange_strong_<T>_explicit(ptr, expected, desired, succ_order, fail_order)`

for `T ∈ {u64, u32, bool, ptr}`. A C2/C3 `boc_atomic_link_t` for
`BOCBehavior *` next-pointers aliases `boc_atomic_ptr_t` with
`(BOCBehavior *)` casts at the call sites.

**MSVC implementation pattern** (illustrative; one per op):

```c
static inline uint64_t
boc_atomic_load_u64_explicit(boc_atomic_u64_t *ptr,
                              boc_memory_order_t order) {
    switch (order) {
#if defined(_M_ARM64)
        case BOC_MO_ACQUIRE: return (uint64_t)__ldar64((unsigned __int64 const volatile *)ptr);
        case BOC_MO_RELAXED: return *ptr;   /* explicit no-fence load */
        default:             return (uint64_t)__ldar64((unsigned __int64 const volatile *)ptr);
#else
        default:             return *ptr;   /* x86/x64 TSO: load is acquire */
#endif
    }
}

static inline uint64_t
boc_atomic_fetch_add_u64_explicit(boc_atomic_u64_t *ptr, uint64_t val,
                                   boc_memory_order_t order) {
#if defined(_M_ARM64)
    switch (order) {
        case BOC_MO_RELAXED: return (uint64_t)_InterlockedExchangeAdd64_nf((volatile __int64 *)ptr, (__int64)val);
        case BOC_MO_ACQUIRE: return (uint64_t)_InterlockedExchangeAdd64_acq((volatile __int64 *)ptr, (__int64)val);
        case BOC_MO_RELEASE: return (uint64_t)_InterlockedExchangeAdd64_rel((volatile __int64 *)ptr, (__int64)val);
        default:             return (uint64_t)_InterlockedExchangeAdd64((volatile __int64 *)ptr, (__int64)val);
    }
#else
    return (uint64_t)_InterlockedExchangeAdd64((volatile __int64 *)ptr, (__int64)val);
#endif
}
```

(POSIX path: every `boc_atomic_*_explicit` is a one-liner macro that
forwards to the corresponding `atomic_*_explicit` from
`<stdatomic.h>` with `BOC_MO_*` translated to `memory_order_*`.
Legacy unsuffixed `atomic_load`/`atomic_store`/etc. used by
pre-C-1 `_core.c`/`_math.c` code stay aliased to their existing
implicit-barrier behaviour for back-compat — only the new
`_explicit` family is required to be ordering-correct.)

**The plan's pseudocode (§G2, C1 mpmcq citations) translates 1:1.**
E.g. `atomic_load_explicit(&unpause_epoch, memory_order_acquire)`
becomes
`boc_atomic_load_u64_explicit(&unpause_epoch, BOC_MO_ACQUIRE)`. The
C1 commit's "side-by-side memory-orderings table" (per the cross-
cutting checklist) is updated to use the `boc_atomic_*` names so
the Verona-line ↔ bocpy-line correspondence remains line-for-line.

**Verona constructs ported.** None. C-1 is pre-Verona-work. (The
MSVC-atomics extension cites no Verona file because Verona uses
C++ `<atomic>` directly; the polyfill is a bocpy-specific port-
support layer.)

**Deviations.** N/A for the mechanical extractions. The MSVC-
atomics extension is a deviation **only from the existing bocpy
polyfill**, not from Verona; it brings the polyfill's
`memory_order_*` semantics from "accepted but ignored" up to
C11-conformant on every supported MSVC target.

**Exit criteria.**

- `pytest -vv` clean on `.env312`, `.env313t`, `.env314`, `.env315`,
  `.env315t`. Same test outcomes (pass/fail) as the parent commit,
  bit-for-bit.
- `flake8 src/ test/` clean (no Python edits, so trivially so).
- Build succeeds on every venv. **`.env315t` (free-threaded) build
  is the canary** — if the extracted polyfills get the macro
  expansion wrong, this venv will catch it first.
- **MSVC build matrix:** the new `compat.{h,c}` compiles cleanly
  under VS 2019 (the floor) and VS 2022 on both x64 and ARM64.
  CI job names: `windows-x64-vs2019`, `windows-x64-vs2022`,
  `windows-arm64-vs2022`. The `windows-arm64-vs2022` job runs the
  full `pytest -vv` suite plus a new
  `test/test_compat_atomics.py` that exercises every
  `boc_atomic_*_explicit` op at every `BOC_MO_*` ordering against
  a 4-thread acquire/release handshake (the canonical ARM64
  weak-memory smoke test). The job is allowed to fail-soft only
  if no ARM64 runner is provisioned for that branch tip; in that
  case the C1/C2/C3 commits inherit a `[needs-arm64-validation]`
  marker until the runner is back.
- **Polyfill-coverage grep:** `grep -RnE
  'atomic_load_explicit\(|atomic_store_explicit\(|atomic_fetch_(add|sub)_explicit\(|atomic_compare_exchange_strong_explicit\(' src/bocpy/`
  returns no hits outside `compat.h` — i.e. all C11-style
  `_explicit` calls in `_core.c`/`message_queue.c`/`sched.c`
  go through `boc_atomic_*_explicit`. (This guards against the
  C1/C2 implementer importing `<stdatomic.h>` directly and
  silently bypassing the MSVC dispatch.)
- **Legacy polyfill preserved:** `grep -RnE
  '\bInterlockedExchangeAdd64\b|\bmemory_order_seq_cst[[:space:]]+0\b'
  src/bocpy/` returns hits **only** inside `compat.c`. The
  unsuffixed `atomic_*` API used by pre-C-1 code remains
  available unchanged for back-compat; the change is purely
  additive.
- `nm -D src/bocpy/_core*.so | sort > .copilot/c-1-symbols-after.txt`
  diffed against the pre-C-1 snapshot shows only **additions** of
  the at-most-handful of newly-exported symbols listed in rule 2
  above plus the new `boc_atomic_*` entry points (which are
  `static inline` in `compat.h` and therefore should not appear in
  `nm` output — their absence is a positive signal that the inliner
  folded them at every call site).
- `git diff --stat` shows the line delta dominated by moves
  (`git diff -M -C`); the MSVC-atomics extension contributes a
  visible ≈200-line addition to `compat.c` and ≈100-line addition
  to `compat.h`.
- Bench parity: GRANDROUGE chain-ring throughput at W ∈ {1, 4, 8}
  within ±1% of the parent commit (a refactor + a polyfill
  extension whose hot path is `static inline` should be perf-neutral
  to noise).

---

### C0 — `sched-instr`: instrumentation + scheduler module skeleton

**Scope.** Add the `sched.{h,c}` translation unit, the per-worker
counters infrastructure (no counters move yet), and the
`_core.scheduler_stats()` accessor. No behavioural change.

**Files touched:**

- `setup.py` — add `src/bocpy/sched.c` to `sources`. (`compat.c`,
  `xidata.c`, `noticeboard.c`, `tags.c`, `terminator.c`,
  `message_queue.c` are already present from C-1.)
- `src/bocpy/sched.h` (new) — opaque `boc_sched_worker_t`, POD
  `boc_sched_stats_t` (initial fields: `pushed_local`, `pushed_remote`,
  `popped_local`, `popped_via_steal`, `enqueue_cas_retries`,
  `dequeue_cas_retries`; counters that are not yet meaningful return
  zero), prototypes for `boc_sched_init(Py_ssize_t worker_count)`,
  `boc_sched_shutdown(void)`, `boc_sched_stats_snapshot(...)`.
  Doc-comments per `commenting-c-and-python`.
- `src/bocpy/sched.c` (new) — per-worker stats array (cacheline-padded);
  `boc_sched_init` / `boc_sched_shutdown` callable from `_core.c`'s
    module init/teardown; `boc_sched_incarnation` counter (G7,
    plain `size_t`).
  - `boc_sched_worker_t` carries a reserved `_Atomic uint64_t
    reserved_phase5;` slot at C2 (placeholder for the per-worker
    terminator delta of Phase 5; doc-commented; zero-initialised;
    pre-counted in the cacheline `static_assert` so adding a real
    counter later does not perturb layout).
- `src/bocpy/_core.c` —
  - Wire `boc_sched_init`/`_shutdown` into the existing module
    init/teardown.
  - Inside the `BOCQueue` struct (now in `message_queue.h` after
    C-1): add four cacheline-padded `_Atomic uint64_t` counters
    (`enqueue_cas_retries`, `dequeue_cas_retries`, `pushed_total`,
    `popped_total`); bump them with `memory_order_relaxed` inside the
    CAS-retry arms of `boc_enqueue` / `boc_dequeue` (now in
    `message_queue.c`) and on success.
  - Add `_core.scheduler_stats() -> list[dict]` returning a snapshot
    of both the BOCQueue counters and the (still-zero) per-worker
    scheduler counters.
- `src/bocpy/__init__.pyi` — stub for `scheduler_stats`.
- `examples/benchmark.py` — `--emit-scheduler-stats` flag.
- `test/test_boc.py` — smoke test: shape of `scheduler_stats()`,
  monotonicity, zero-side-effect on call.

**Verona constructs ported.** Stats POD modelled on
[`schedulerstats.h`](../../verona-rt/src/rt/sched/schedulerstats.h)
(subset; expanded in C3).

**Deviations.** Stats counters are populated bottom-up across
commits, not in a single drop, because Verona shipped `SchedulerStats`
complete alongside its complete scheduler — bocpy is staging.

**Exit criteria.**

- `pytest -vv` clean on `.env312`, `.env313t`, `.env314`, `.env315`,
  `.env315t`.
- `flake8 src/ test/` clean.
- Build succeeds in every venv.
- Baseline chain-ring throughput on GRANDROUGE within ±2% of
  `dist-sched-final` at W ∈ {1, 4, 8}.
- `--emit-scheduler-stats` baseline contention measured: at W=8 on
  GRANDROUGE, `enqueue_cas_retries / pushed_total ≥ 0.05` on the
  null-payload chain-ring. Raw JSON committed under
  `.copilot/plans/work-stealing-scheduler/results/c0/`.

---

### C1 — `sched-mpmcq`: port `MPMCQ<BOCBehavior *>` verbatim

**Scope.** Add the queue primitive and a C-level TSAN stress test.
No production caller. Adds the `next_in_queue` field to `BOCBehavior`.

**Files touched:**

- `src/bocpy/sched.h` —
  - Forward-declare `struct BOCBehavior`.
  - Declare `boc_bq_t` (queue head: `_Atomic(BOCBehavior *) front`,
    `_Atomic(_Atomic(BOCBehavior *) *) back`, padded for false-sharing).
  - Declare `boc_bq_init`, `boc_bq_destroy_assert_empty`,
    `boc_bq_enqueue`, `boc_bq_enqueue_segment`, `boc_bq_dequeue`,
    `boc_bq_dequeue_all`, `boc_bq_acquire_front`, `boc_bq_is_empty`.
  - `BATCH_SIZE` constant (`static const size_t BATCH_SIZE = 100;`).
  - Doc-comments cite the Verona line each function ports.
- `src/bocpy/sched.c` — implementation. Memory orderings copied
  verbatim from
  [`mpmcq.h`](../../verona-rt/src/rt/sched/mpmcq.h):
  - `back.exchange(..., memory_order_acq_rel)` in `enqueue_segment`
    (mpmcq.h:104).
  - `next_in_queue.store(..., memory_order_release)` after the
    exchange (mpmcq.h:113).
  - `front.exchange(NULL, memory_order_acquire)` in `acquire_front`
    (mpmcq.h:53).
  - `next_in_queue.load(memory_order_acquire)` in `dequeue` /
    `Segment::take_one` (mpmcq.h:78, 145).
  - `back.compare_exchange_strong(..., acq_rel, relaxed)` close-queue
    rollback (mpmcq.h:162).
  - `front.store(..., memory_order_release)` rollback (mpmcq.h:174).
  - `back.exchange(&front, memory_order_acq_rel)` in `dequeue_all`
    (mpmcq.h:192).
  - Empty representation `back == &front` (mpmcq.h:36, 191).
  - `BOC_SCHED_YIELD()` macro at every `Systematic::yield()` call
    site in Verona — expands to nothing in release builds, to
    `sched_yield()` under `-DBOC_SCHED_SYSTEMATIC`. Preserves the
    schedule-perturbation points the Verona authors validated against.
- `src/bocpy/_core.c` — add `_Atomic(struct BOCBehavior *) next_in_queue;`
  to `BOCBehavior` (line ~4121). Placement: **inside the existing
  first cacheline alongside `count` and `thunk`** if `pahole` shows it
  shares the line only with cold fields; otherwise at struct end.
  Do **not** make it the first field — that would shift every existing
  offset and invalidate the cacheline-layout doc-comments at lines
  ~4115 / ~4145 (Verona's `Work` layout is determined by C++
  inheritance, not by the application putting the link first;
  citing `work.h` for field order is unsupported). Update every
  doc-comment that references field ordering in this commit.
  Initialise to `NULL` in `behavior_prepare_start` (line ~4530)
  **before** the behaviour becomes reachable from any other thread —
  this preserves the link-loop infallibility invariant from constraint
  #3 because the field is initialised before the GIL is released.
- `test/test_scheduler_mpmcq.py` (new) — C-level driver: hammer
  `enqueue` from N=8 producer threads against a single `dequeue`
  consumer plus a second consumer running `dequeue_all` +
  `acquire_front`. Run for ≥10⁶ iterations. Drive from pytest via a
  small `_core` test entry point.
- The PR description includes the side-by-side memory-orderings table
  from usability's mpsc-vs-mpmc rebuttal §4 (verbatim Verona line vs.
  bocpy line).

**Verona constructs ported.** `MPMCQ<T>` end-to-end
([`mpmcq.h`](../../verona-rt/src/rt/sched/mpmcq.h)),
`Work::next_in_queue` ([`work.h`](../../verona-rt/src/rt/sched/work.h)).

**Deviations.** None at the queue level. The intrusive field placement
in `BOCBehavior` is `pahole`-driven (above), not a Verona-convention
claim.

**Exit criteria.**

- `pytest -vv test/test_scheduler_mpmcq.py` passes on `.env314`,
  `.env315t` (free-threaded — the memory-model gate).
- TSAN run on `.env315t`: `pytest -vv test/test_scheduler_mpmcq.py`
  with `-fsanitize=thread` clean. Output captured to
  `.copilot/plans/work-stealing-scheduler/c1-tsan.txt`.
- `pahole src/bocpy/_core.so` confirms `BOCBehavior` first cacheline
  contains `next_in_queue`, `count`, and `thunk`.
- Full pre-existing test suite still green (no production caller of
  the queue yet, so this should be free).
- `flake8` clean.

---

### C2 — `sched-perworker-handoff`: per-worker queue + `pending`/batch + producer-locality

**Scope.** Wire the queue from C1 into `behavior_resolve_one` and
`request_release_inner`. Introduce `BOCWorker`. Add the `pending`
slot, `BATCH_SIZE` accounting, the `current_worker` TLS handle, and
the round-robin TLS cursor for off-worker producers. Replace the
`receive("boc_worker")` path in `worker.py::do_work` with
`_core.boc_sched_worker_pop()`. Implement the park / shutdown protocol
from G2 + D4. Remove the `start_message` pre-allocation from
`behavior_prepare_start` (the behaviour itself is now the queue node).

**Files touched:**

- `src/bocpy/sched.h` —
  - `boc_sched_worker_t` becomes concrete: `boc_bq_t q;`
    `_Atomic(BOCBehavior *) token_work; /* set in C3 */`
    `_Atomic(bool) should_steal_for_fairness;` (declared in C2;
    zero-initialised via `PyMem_Calloc`; **first non-zero write and
    first read both land in C3** alongside `try_steal` wiring — keeps
    C2's bisect surface free of dead initialisation logic); `_Atomic(bool) stop_requested;` `Py_ssize_t owner_interp_id;`
    `pthread_mutex_t cv_mu; pthread_cond_t cv;` `BOCWorker *next_in_ring;`
    plus the per-worker `boc_sched_stats_t`. Cacheline-padded;
    `static_assert(sizeof(boc_sched_worker_t) % 64 == 0)`.
  - `static inline void boc_sched_dispatch(struct BOCBehavior *b);` —
    body in the header so `_core.c` callers inline it. The body reads
    the `current_worker` TLS handle and branches: producer-locality
    path (Verona `schedule_fifo` semantics: **always** evict the prior
    `pending` to the local queue and replace it with `b`; `BATCH_SIZE`
    accounting is consumer-side only,
    [`schedulerthread.h:86-101`](../../verona-rt/src/rt/sched/schedulerthread.h#L86)
    + [`schedulerthread.h:122-138`](../../verona-rt/src/rt/sched/schedulerthread.h#L122))
    vs round-robin path (TLS cursor walks the worker ring, falls back
    to a fresh re-seed when `boc_sched_incarnation` changes). After
    the publish, the slow arm runs the `pause`/`unpause`-aware wake
    sketched in §G2.
  - `static inline BOCBehavior *boc_sched_worker_pop_fast(...)` — fast
    path (pending, then own queue). Slow path (`boc_sched_worker_pop_slow`)
    stays in `sched.c`.
  - Declare `boc_sched_worker_register(Py_ssize_t my_index)` and
    `boc_sched_worker_request_stop_all(void)` and
    `boc_sched_worker_pop(void)` (the Python-callable wrapper).
- `src/bocpy/sched.c` —
  - Allocate `boc_sched_worker_t workers[worker_count]` in
    `boc_sched_init`; link into a ring via `next_in_ring`.
  - `_Thread_local boc_sched_worker_t *current_worker = NULL;`
    `_Thread_local BOCBehavior *pending = NULL;`
    `_Thread_local size_t batch = BATCH_SIZE;`
    `_Thread_local size_t rr_incarnation = 0;`
    `_Thread_local boc_sched_worker_t *rr_nonlocal = NULL;`
    All declared with the **default** TLS model (G6).
  - `static _Atomic(uint64_t) sched_epoch;`
    `static _Atomic(uint32_t) parked_count;` and per-worker
    `parked` flag (G2 `pause`/`unpause` port).
  - `boc_sched_unpause_one(self)` — walk the worker ring once from
    `self->next_in_ring`; for the first worker whose `parked` flag is
    set, lock its `cv_mu` and `cond_signal` it. Verona equivalent:
    `ThreadPool::unpause`.
  - `boc_sched_worker_pop_slow` implements the park protocol from G2
    (mutex + recheck + `pthread_cond_wait`); maintains `parked_count`
    and the per-worker `parked` flag; drops the GIL across
    `pthread_cond_wait` so other Python work can proceed. **Does not
    consult `terminator_count`** — only `stop_requested` causes exit.
  - `boc_sched_worker_request_stop_all` sets `stop_requested` on every
    worker and broadcasts each `cv` (D4 / Verona
    [`threadpool.h:335-340`](../../verona-rt/src/rt/sched/threadpool.h)).
  - The producer signal (lock-then-`cond_signal` for cross-worker
    publish, or `boc_sched_unpause_one` for producer-local publish
    when `parked_count > 0`) lives inside `boc_sched_dispatch`'s slow
    arm.
- `src/bocpy/_core.c` —
  - `behavior_resolve_one` (~line 4494): replace the
    `boc_enqueue(start_message)` with `boc_sched_dispatch(behavior)`.
    All four callers (lines ~4646, ~4812, ~5200, ~5227 — see §G1
    table) now reach the new dispatch path; only the line-5200 caller
    is guaranteed to take the producer-locality branch (worker thread,
    non-NULL `current_worker`).
  - `behavior_prepare_start` (~line 4530): retain the `start_message`
    field for this commit; set it to `NULL` and assert it stays NULL
    through teardown (a follow-up commit on this same branch deletes
    the field once the assertion has run green on every venv). This
    keeps the bisect signal clean if a teardown path was missed.
    Constraint #3 still holds because `boc_sched_dispatch` is
    allocation-free, and per §G8 the consumer-side wrap is a
    `PyCapsule` (effectively infallible).
  - Per §G8: producer takes `BEHAVIOR_INCREF` before
    `boc_sched_dispatch`; consumer's `boc_sched_worker_pop` allocates
    a `PyCapsule` in the worker interpreter and transfers the
    reference.
  - Expose `_core.boc_sched_worker_register(my_index)` and
    `_core.boc_sched_worker_pop()`. The latter blocks; releases the
    GIL across the pop.
- `src/bocpy/worker.py::do_work` — switch from
  `receive("boc_worker")` to the protocol in G4. Cleanup unchanged.
- `src/bocpy/behaviors.py` — `start_workers` calls
  `_core.boc_sched_worker_register(self.index)` once per worker
  bootstrap; `stop_workers` calls
  `_core.boc_sched_worker_request_stop_all()` then drains and joins.
- `src/bocpy/__init__.pyi` — stubs per G5.
- `test/test_scheduler_pertask_queue.py` (new) —
  - Single-worker FIFO (1000 behaviours from main thread, run in order).
  - Local hand-off: chained behaviours show `pushed_local` ticking and
    `pushed_remote == 0`.
  - `BATCH_SIZE` consume-side: chain of `BATCH_SIZE + 5` behaviours
    forces at least one consume-side batch reset (`pending` evict on
    every dispatch is per `schedule_fifo`; the batch cap lives in the
    consumer pop loop).
  - Round-robin from main thread: 8 behaviours land on at most 8
    distinct workers.
  - **Producer-on-other-worker → parked-worker liveness (G2):** start
    `worker_count = 2`; from the main thread, schedule a behaviour
    that sleeps 50 ms (long enough for both workers to park) and
    then schedules `@when(c1)`; assert the second behaviour runs
    (the parked target is woken by the cross-worker `cond_signal`).
  - **Producer-local publish wakes a parked peer (G2 unpause):**
    pin a chain to worker A via producer-locality; concurrently
    leave worker B parked. Assert B wakes within a bounded time
    (validates `boc_sched_unpause_one`).
  - **TLS-slot exercise per venv (G6):** for each registered worker,
    assert `boc_sched_stats_snapshot()` shows non-zero `pushed_local`
    after one trivial behaviour. Forces every TLS slot
    (`pending`/`batch`/`current_worker`/`rr_*`) to be touched;
    surfaces `dlopen`-time TLS-load failures on `.env312`,
    `.env313t`, `.env314`, `.env315`, `.env315t`.
  - **G8 paired-release contract test (constraint #3):** a `@when`
    body raises an uncaught `RuntimeError` (bypassing
    `Cown.exception`); assert `wait()` returns, the cown is
    re-acquirable by a follow-on behaviour, and at teardown the
    per-worker capsule's pointer-is-NULL assertion holds.
  - **C2 parked-peer CPU exit criterion:** at W=2, with all work
    pinned to worker 0 via producer-locality, sample worker 1's CPU
    usage over 2 s; assert ≤ 5% (the parker self-only `check_for_work`
    in C2 must let peers actually sleep when they cannot help).
  - Shutdown: `stop()` mid-dispatch drains correctly, no behaviour
    leaks (`boc_bq_destroy_assert_empty` does not fire).
  - **`start()`/`wait()`/`start()` re-entry:** run a workload to
    quiescence, then call `start()` again and run another workload.
    Workers must not have exited on the transient `terminator_count
    == 0` (validates D4 + G2 decoupling).
  - Wrong-index `boc_sched_worker_register` raises `RuntimeError`.
- `test/test_scheduling_stress.py` — parameterise over
  `worker_count ∈ {1, 2, 4, 8}`; new chain-ring of length 10 000;
  assert no leaks at teardown, terminator drift = 0.

**Verona constructs ported.**

| bocpy | Verona |
|---|---|
| `boc_sched_worker_t` | `Core` ([`core.h`](../../verona-rt/src/rt/sched/core.h)) |
| `pending` + `batch` (TLS) | `next_work` + `BATCH_SIZE` ([`schedulerthread.h:55-60, 122-138`](../../verona-rt/src/rt/sched/schedulerthread.h)) |
| `boc_sched_dispatch` producer-locality branch | `schedule_fifo` ([`schedulerthread.h:86-101`](../../verona-rt/src/rt/sched/schedulerthread.h)) |
| `boc_sched_dispatch` round-robin branch | `ThreadPool::round_robin` ([`threadpool.h:147-167`](../../verona-rt/src/rt/sched/threadpool.h)) |
| `rr_incarnation` / `rr_nonlocal` | identical TLS pattern, same file |
| Stop flag + condvar broadcast | `threadpool.h:335-340` (`stop()` + `unpause_all`) |
| `boc_bq_destroy_assert_empty` | `~MPMCQ()` assert ([`mpmcq.h:218-222`](../../verona-rt/src/rt/sched/mpmcq.h)) |

**Deviations.**

1. **Per-worker condvar instead of global `ThreadSync`.** Verona uses
   one global condvar
   ([`threadsync.h`](../../verona-rt/src/rt/sched/threadsync.h)) so
   that `unpause_all` is one broadcast. Per-worker condvars cost a
   broadcast loop on shutdown but win on every steady-state new-work
   wake (one signal targeting one worker, not a wakeup storm). The
   bocpy precedent (existing `BOCQueue` waiters) is per-queue
   condvars; we follow it. The cross-worker liveness gap that
   per-worker condvars introduce is closed by porting Verona's
   `pause`/`unpause` epoch dance — see G2.
2. **`pending`/batch/`current_worker`/round-robin cursor are
   `_Thread_local` instead of `SchedulerThread` fields.** Each worker
   is a sub-interpreter with its own OS thread, so TLS gives the same
   effect with one fewer indirection. TLS model is default
   (`global-dynamic`), per G6.
3. **`token_work` field allocated and zeroed but inert until C3,
   and `should_steal_for_fairness` declared zero-initialised in C2
   with first write/read in C3.**
   Same pattern as the Verona stages — the field exists; the
   `should_steal_for_fairness` flag's consumer (re-enqueue + steal)
   is wired in C3. Justification: keeps C3's diff focused on the
   steal protocol; avoids dead-init mismatches between commits.
4. **`enqueue_front` (LIFO) for external schedules NOT ported.**
   Verona uses it for I/O completions
   ([`schedulerthread.h::schedule_lifo`](../../verona-rt/src/rt/sched/schedulerthread.h));
   bocpy has no asynchronous external event source today. FIFO
   preserves benchmark determinism. Revisit if such a source appears.

**Exit criteria.**

- `pytest -vv` passes on `.env312`, `.env313t`, `.env314`, `.env315`,
  `.env315t`.
- `flake8` clean.
- TSAN on `.env315t` clean for the new tests.
- Counters: `local_handoff_hits / behaviours_dispatched ≥ 0.90` on
  chain-ring at W=8; `enqueue_cas_retries` near zero per behaviour
  on chain-ring (single producer per worker queue most of the time).
- No leaked behaviours across 1000 random `wait()` cycles in stress.
- W=1 chain-ring throughput within ±5% of baseline (producer-locality
  recovers locality; per-worker queue is uncontended).

(Branch-tip perf targets are deferred to C3.)

---

### C3 — `sched-stealing`: work-stealing + `token_work` fairness

**Scope.** Activate `should_steal_for_fairness`. Implement
`try_steal(thief)` and `steal(thief)`. Allocate the per-worker
`token_work` sentinel. Extend `boc_sched_worker_pop` to its full
Verona shape (`pending` → token check → own queue → `try_steal` ring
→ park).

**Files touched:**

- `src/bocpy/sched.c` —
  - Allocate `token_work` per worker in `boc_sched_init`: a real
    `BOCBehavior` from `PyMem_Calloc`; `is_token = 1`; `count = 0`;
    `thunk = NULL`; never reference-counted; freed in
    `boc_sched_shutdown`.
  - Add `uint8_t is_token` to `BOCBehavior` in `_core.c` (placed in
    existing padding so struct size unchanged; `pahole` verified).
  - **Two functions, matching Verona's split** (no merging):
    - `try_steal(thief)` — fast, **single-victim** attempt against
      `thief->victim` (then advance the cursor); returns NULL on
      failure ([`schedulerthread.h:233-251`](../../verona-rt/src/rt/sched/schedulerthread.h#L233)).
      Called from the empty-queue arm of `get_work`-equivalent.
    - `steal(thief)` — slow, multi-victim loop with `BOC_SCHED_YIELD()`
      between rounds and a `clock_gettime(CLOCK_MONOTONIC)`-based
      quiescence timeout ([`schedulerthread.h:257-310`](../../verona-rt/src/rt/sched/schedulerthread.h#L257)).
    - Each victim pass: `boc_bq_dequeue_all(&victim->q)`
      ([`mpmcq.h:dequeue_all`](../../verona-rt/src/rt/sched/mpmcq.h)
      + [`workstealingqueue.h:steal`](../../verona-rt/src/rt/sched/workstealingqueue.h));
      return one item, splice the rest onto the thief's own queue.
  - `boc_sched_worker_pop_slow` reordered to mirror Verona's
    `get_work` ([`schedulerthread.h:122-167`](../../verona-rt/src/rt/sched/schedulerthread.h)):
    on `pending`-empty + `should_steal_for_fairness`, attempt one
    steal ring **before** the local-queue dequeue, re-enqueue
    `token_work`, clear the flag.
  - The token's "thunk" is the C-level no-op
    `boc_token_thunk(BOCWorker *self) { self->should_steal_for_fairness = true; }`
    invoked at the dequeue site behind a single
    `if (b->is_token)` check.
  - On park (the same protocol from G2), bound steal retries at
    `worker_count - 1` before `pthread_cond_wait` — Verona equivalent
    in `try_steal` returning NULL after one ring.
  - New counters: `popped_via_steal`, `steal_attempts`,
    `steal_failures`, `parked`, `last_steal_attempt_ns`. Wired into
    `boc_sched_stats_snapshot`.
- `src/bocpy/__init__.pyi` — extend `scheduler_stats` docstring per G5.
- `test/test_scheduler_steal.py` (new) —
  - Pin all work to worker 0 with affinity; assert workers 1..N-1
    show `popped_via_steal > 0` and the run completes.
  - Token-work fairness: chain of `> BATCH_SIZE` items provokes ≥1
    steal pass.
  - Empty-queue race: spawn `worker_count` workers and 0 work; assert
    all park, none spin.
  - Spurious-failure stress: with `-DBOC_SCHED_SYSTEMATIC`, run 100
    iterations; all converge.
- TSAN run captured to
  `.copilot/plans/work-stealing-scheduler/c3-tsan.txt`.

**Verona constructs ported.** `try_steal`, `steal`, `get_work` ordering
([`schedulerthread.h`](../../verona-rt/src/rt/sched/schedulerthread.h)),
`token_work` + `should_steal_for_fairness`
([`core.h:22-37`](../../verona-rt/src/rt/sched/core.h)),
`acquire_front` / `dequeue_all` already present from C1.

**Deviations.**

1. **`steal_index` is per-thief (cross-worker cursor) only, no
   per-WSQ index.** Justification: there is still one queue per worker
   (N=1) until C4. The intra-WSQ index has no meaning. C4 reintroduces
   it.
2. **No `Core::next` linked list runtime mutation.** The worker ring
   is the worker array indexed modulo `worker_count`, immutable per
   G3.
3. **Backoff is `clock_gettime(CLOCK_MONOTONIC)` + `BOC_SCHED_YIELD()`
   instead of Verona's `DefaultPal::tick()`.** TSC abstraction is not
   portable to all bocpy build targets.
4. **None on the `try_steal` / `steal` split** — both functions are
   ported per Verona; bocpy's combined-loop variant from earlier
   drafts is rejected (loses GIL-yield points and the quiescence
   timeout).

**Exit criteria.**

- All previous tests still pass on every venv.
- TSAN clean on `.env315t`.
- `wait()` returns in 1000-iteration quiescence stress (G2 protocol
  validated under steal load).
- **Branch-tip perf gate (GRANDROUGE, null-payload chain-ring):**
  - W=1: ≥ 0.95× of `dist-sched-final` baseline.
  - W=2: ≥ 1.50×.
  - W=4: ≥ 3.0× (vs W=1).
  - W=8: ≥ 6.4× (vs W=1) — the headline ≥80%-linear target from
    sketch §5 phase 3; ≥7× is the stretch goal.
- ROJOGRANDE (single-NUMA): no regression on any benchmark
  (validates that stealing does not over-fire where it was not
  needed).
- `popped_via_steal / behaviours_dispatched ≤ 0.05` on chain-ring
  (steady state stays local; stealing is rare-event fairness only).

---

### C4 — `sched-wsq-fanout` (gated): `WorkStealingQueue<N=4>` per worker

**Scope (conditional).** Lands **only if** C3 stats on a fan-out
benchmark show non-trivial `enqueue_cas_retries` on the per-worker
queue back pointer. If contention is already ≤1% of pushes, this
commit is dropped from the branch.

**Files touched (if landed):**

- `src/bocpy/sched.h` / `sched.c` — replace `boc_bq_t q;` in
  `boc_sched_worker_t` with `boc_bq_t q[BOC_WSQ_N]` (default
  `N = 4`, matching Verona). Add three `WrapIndex` cursors:
  `enqueue_index`, `dequeue_index`, `steal_index`. Port
  `enqueue_spread` from
  [`workstealingqueue.h`](../../verona-rt/src/rt/sched/workstealingqueue.h)
  so a steal redistributes the stolen segment round-robin across the
  thief's sub-queues.
- `test/test_scheduler_fanout.py` (new) — `worker_count × 4` producer
  threads scheduling onto the same target; assert per-sub-queue
  counters bounded and total throughput ≥ C3 baseline.
- `examples/benchmark.py` — fan-out benchmark variant.

**Verona constructs ported.** `WorkStealingQueue<N>` and `WrapIndex<N>`
verbatim
([`workstealingqueue.h`](../../verona-rt/src/rt/sched/workstealingqueue.h),
[`ds/wrapindex.h`](../../verona-rt/src/rt/ds/wrapindex.h)).

**Deviations.** None planned.

**Exit criteria.**

- All prior tests + the new fan-out test pass.
- Boids and prime-factor examples show no regression.
- Chain-ring scaling unchanged from C3 (≤2% delta either way).
- Per-sub-queue counters nest under a `subqueues: list[dict]` key in
  the per-worker stats dict; top-level keys unchanged (G5).

---

## Cross-cutting checklist (applied to every commit before branch tip)

1. `pahole src/bocpy/_core.so` — verify `BOCBehavior`, `BOCWorker`,
   and `boc_bq_t` cacheline layout. No false sharing on hot lines.
2. `objdump -d` — verify the worker fast-path does not emit
   `__tls_get_addr` more than necessary (G6).
3. `flake8 src/ test/` clean.
4. Sphinx-style docstrings on every new C function and stub
   (`commenting-c-and-python` skill).
5. `.env315t` (free-threaded) `pytest -vv` passes.
6. PR description (or branch summary) includes the side-by-side
   memory-orderings table for any new `mpmcq.h` code. The bocpy
   side of the table uses the `boc_atomic_*_explicit` API from
   `compat.h` (introduced in C-1) so the citation reads identically
   on POSIX and MSVC.
7. **MSVC build matrix green** on `windows-x64-vs2019`,
   `windows-x64-vs2022`, and `windows-arm64-vs2022` (fail-soft if
   no ARM64 runner is provisioned, with `[needs-arm64-validation]`
   marker carried until it returns). Any commit that adds a new
   `boc_atomic_*_explicit` call site re-runs `test/test_compat_atomics.py`
   on the ARM64 runner.
8. **Polyfill-coverage grep** (`atomic_*_explicit\(` outside
   `compat.h` returns no hits) is re-checked on every commit.
9. GRANDROUGE + ROJOGRANDE bench JSONs and scaling-curve plots
   committed under
   `.copilot/plans/work-stealing-scheduler/results/<commit>/`.

---

## Unresolved risks

1. **MSVC `_explicit`-suffix ARM64 intrinsic availability.** The
   C-1 polyfill prefers the suffixed `_InterlockedExchangeAdd64_acq`
   / `_rel` / `_nf` and `__ldar64` ARM64 variants, but their SDK
   coverage is not uniform across every supported VS 2019/2022
   toolchain level. **Selection is preprocessor-gated**, not runtime
   discovery: `compat.h` picks the cheap suffixed intrinsic when
   `defined(_M_ARM64) && _MSC_VER >= <floor>` (floor TBD per
   intrinsic during C-1 implementation, with `<intrin.h>` /
   `<arm64intr.h>` as the authoritative source), and falls back to
   the unsuffixed full-barrier `Interlocked*` otherwise. The fallback
   path is correct (full barrier is always safe in place of acq/rel/
   relaxed) — only ARM64 perf is affected. Each fallback site carries
   a `// TODO(arm64-msvc-sdk):` comment with the gating macro so the
   floor can be raised when the MSVC support story stabilises. No CI
   matrix is required to *catch* the issue (the preprocessor decides
   at compile time on every build); the matrix exists to *measure*
   the resulting code-gen.

2. **`__tls_get_addr` overhead under sub-interpreters / free-threaded
   loading.** G6 commits to default TLS model. If C3 measurement on
   `.env315t` shows the cost is non-negligible, the fallback is to
   pack `pending`/`batch`/`current_worker` into a single TLS struct
   pointer (one TLS lookup, multi-field read), not to promote to
   `initial-exec`.

2. **`pause`/`unpause` correctness under sub-interpreter teardown.**
   The G2 port is mandatory in C2 (see §G2). The known sharp edge is
   the interaction between `parked_count` and per-interpreter
   teardown order during `stop()`: if a worker's interpreter is torn
   down before its `parked` flag is cleared, `parked_count` could
   drift. Mitigation: `stop_workers` waits for every worker to
   acknowledge `stop_requested` (existing `boc_cleanup` ack path)
   **before** any sub-interpreter is destroyed. Validated by the
   1000-cycle quiescence stress in C3.

3. **Terminator remains the next contention point.** Out of scope per
   `00-context.md`; once C3 lands, `TERMINATOR_COUNT` becomes the
   dominant shared cacheline by construction. The C2 `BOCWorker`
   layout includes the reserved `reserved_phase5` slot pre-counted in
   the cacheline `static_assert` so the future Phase 5 commit does
   not perturb layout.

4. **C4 gating depends on a fan-out benchmark we still need to write.**
   The decision to land or drop C4 is data-driven; if the
   benchmark itself is methodologically wrong we may make the wrong
   call. Mitigation: the fan-out benchmark is added in C3's test/bench
   delta, not in C4, so it is reviewed before its results are used as
   a gate.

## Unresolved disagreements

None. Every disagreement listed in
[`20-analysis.md`](20-analysis.md) §"Points of disagreement" was
resolved above by engaging with the rebuttal arguments. The factual
question on D7 (does Verona use a global atomic or TLS for
`round_robin`?) was decided by inspecting
[`threadpool.h:147-167`](../../verona-rt/src/rt/sched/threadpool.h),
which confirmed the speed lens's reading.
