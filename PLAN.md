# bocpy — Structured Concurrency PR Plan

> **Status:** drafted, internally reviewed (3-pass adversarial loop), ready for team review before implementation.
>
> **Reviewers:** please focus on the *Design decisions* and *Open risks for review* sections below before drilling into the per-step plan further down. The 10-step plan is detailed for the implementer; you do not have to digest every file path.
>
> **Companion docs.** The authoritative design is the sketch at
> [.sketches/behaviors-context-manager.md](./.sketches/behaviors-context-manager.md);
> the planning artifacts (lens plans, rebuttals, analysis, adversarial passes) live in
> [.copilot/plans/structured-concurrency/](./.copilot/plans/structured-concurrency/).

---

## TL;DR for Pyrona readers

bocpy gives you `@when(*cowns)` and a process-global runtime: behaviors run as soon as their cowns are free, and `bocpy.wait()` blocks until *everything in the process* has quiesced. That coarse rendezvous is fine for scripts and demos, but it makes it impossible to say *"wait for **this** chunk of work to finish"* without also waiting for unrelated work the same process happens to be doing — and it makes it impossible to write a library that internally schedules behaviors without leaking that fact to its callers.

This PR adds **structured concurrency** to bocpy: a first-class `Terminator` primitive plus a `with behaviors():` context manager that scope work, propagate transitively across `@when` boundaries (and through cown-to-cown / worker-to-worker hops), and produce per-scope `stats` and `noticeboard` snapshots.

```python
import bocpy

c = bocpy.Cown(0)

with bocpy.behaviors() as scope:
    @bocpy.when(c)
    def increment(c):
        c.value += 1

    @bocpy.when(c)               # transitively inherits `scope`
    def announce(c):
        print(f"c is {c.value}")

# scope.__exit__ blocks until both behaviors finish.
# Any *other* behaviors elsewhere in the process keep running undisturbed.
```

A behavior scheduled from inside the scope — even via a deep chain of `@when` callbacks running on a worker sub-interpreter — sees the same `Terminator` through `contextvars`, so the scope correctly waits for all transitive work. You can opt into snapshots:

```python
with bocpy.behaviors(stats=True, noticeboard=True) as scope:
    do_pipeline()

scope.stats        # scheduler-stats delta captured at quiescence
scope.noticeboard  # noticeboard snapshot taken on quiescence
```

A scope can also collect a **single output value** from the behaviors that run inside it. Pass the scope itself as one of a behavior's cowns; inside the body it is acquired exactly like any other cown, and assigning `scope.value` writes the slot under exclusive access:

```python
with bocpy.behaviors() as scope:
    @bocpy.when(a, b, scope)
    def summarise(a, b, scope):
        scope.value = a.value + b.value

# scope.__exit__ waits for quiescence, then moves the output slot's
# final value out of the internal cown and onto scope.value.
print(scope.value)          # None if no behavior wrote it
```

Because writers acquire the scope like a cown, all output-writing behaviors serialise on it (last write wins for the single slot; that same discipline makes `scope.value = scope.value + x` reductions race-free). A behavior that raises does **not** touch the output slot — its exception lands on that behavior's own result cown, so multiple writers never contend over "which exception wins".

You can also construct a `Terminator` directly and pass it explicitly:

```python
t = bocpy.Terminator(stats=True)

@bocpy.when(c, terminator=t)
def isolated(c):
    ...

assert t.wait(timeout=2.0)
print(t.stats)
```

## What changes for users (and what doesn't)

| Surface | Status |
|---|---|
| `@when`, `Cown`, `whencall`, `send`/`receive`, noticeboard | **Unchanged.** Same call sites, same semantics. |
| `@when(*cowns, terminator=t)` | **New** keyword. If omitted, the behavior inherits the calling context's terminator (which defaults to the system terminator → today's behaviour). |
| `bocpy.Terminator(*, stats=False, noticeboard=False)` | **New** public type. Behaves like a scope handle: `wait(timeout)`, `count`, `stats`, `noticeboard`. |
| `with bocpy.behaviors(...) as scope:` | **New** context manager. `scope` is a `Terminator`. |
| `@when(*cowns, scope)` (scope passed positionally) | **New.** Listing a scope among a behavior's cowns acquires that scope's output slot; the body assigns `scope.value` under exclusive access. At most one scope per behavior; the scope also drives that behavior's terminator. |
| `scope.value` | **New.** Single output slot, `None` until a behavior writes it. Populated on `__exit__` by moving the internal output cown's final value onto the scope. |
| `bocpy.wait()`, `bocpy.quiesce()` | **Unchanged signature**, but raise `RuntimeError` if called from inside a `with behaviors():` block (Q2 — that pattern always deadlocks). |
| `Behaviors.start()` | **Unchanged signature**, but raises `RuntimeError` if a user `Terminator` is still alive (Q1 — would race with the new terminator's seed). |
| Reserved keyword `terminator` | A user behavior body declaring a parameter named `terminator` raises `TypeError` at decoration time. |
| Public C ABI (`src/bocpy/include/bocpy/*.h`) | **Untouched.** A finalize-time grep enforces zero references to `terminator`/`Terminator`. Downstream C extensions need no changes. |

## How it works under the hood

The C runtime already has a process-global *terminator* — a count/seeded/closed rundown counter that `bocpy.wait()` blocks on. This PR generalises that into a first-class `boc_terminator_t` heap object with `rc`/`parent`/`alive_children` plumbing, gives every `BOCBehavior` a strong ref to its terminator, and installs a `PyContextVar_Set` / `Reset` bracket on the worker around each thunk so any nested `@when` inherits the right terminator without the user having to thread it through manually.

The resolution chain at `whencall` is:

1. explicit `terminator=` kwarg
2. else `_current_terminator.get()` from contextvars
3. else the cached system terminator

That gives transitive propagation across `@when` boundaries, worker boundaries, and `send`/`receive` boundaries — anywhere a contextvar would naturally flow.

`stats` and `noticeboard` snapshots ride on the same machinery: `stats=True` is captured in C inside the wake path on the `count → 0` transition (one relaxed-atomic flag read on the hot side); `noticeboard=True` is captured in Python by the waiter after a successful `wait()`, with fallbacks for worker-side and post-`stop()` callers.

The **output value** rides on cowns, not the terminator: each scope owns an internal `Cown(None)`. When a scope object appears in a behavior's cown list, `whencall` substitutes that internal cown into the acquire set (so the body's `scope` parameter is really the output cown, written as `scope.value`) and binds the behavior's terminator to that scope so the write and the `__exit__` wait provably target the same rundown counter. On `__exit__`, after `wait()` returns, the scope unwraps its internal cown's final value onto `scope.value`. The write is committed-at-quiescence for free — it happened under acquisition, and quiescence waits for release — so there is no snapshot-barrier race.

## Risk profile and review focus

The plan lands in **11 commits**. Roughly:

| Step | Risk | What it does |
|---|---|---|
| 0 | none | Baseline tests, lint, and benchmarks in the chosen venv. |
| 1 | low | Promote the terminator to a heap struct; in-place rename of every `terminator_*` C function. Behaviour unchanged — everything resolves to the system terminator. |
| 2 | low | Add `rc`/`parent`/`alive_children` primitives **off the production path**, behind `BOCPY_BUILD_INTERNAL_TESTS`. Tested in isolation before any caller depends on them. |
| 3 | **highest** | Give `BOCBehavior` a terminator pointer; collapse three Python-side `terminator_dec` sites into one canonical C dec inside `behavior_release_all_impl`. This is the most-traversed code path in the runtime. |
| 4 | low | Add the public `Terminator` type. `stats=`/`noticeboard=` are *reserved* — set the signature, raise `NotImplementedError` in the body. Locks the public API surface so step 8/9 are body-only. |
| 5 | **high** | Install the C contextvar bracket via a two-branch `behavior_terminator_wrapper(b)` helper. The contextvar storage is the most likely free-threaded hazard in the PR; this step runs on a free-threaded venv before merging. |
| 6 | low | `terminator=` kwarg + reserved-name collision check + rollback-dec switch. Most of the user-facing surface lands here. |
| 7 | medium | `behaviors()` context manager + Q1 (alive-blocks-start) + Q2 (wait-inside-scope-raises) + Q3 enforcement tests. |
| 8 | medium | `Terminator(stats=True)` body — C wake-path snapshot. Isolated commit; a stats regression bisects here. |
| 9 | medium | `Terminator(noticeboard=True)` body — waiter-thread cycle with primary-interpreter and post-stop fallbacks. Isolated commit. |
| 10 | medium | Scope output value: each scope owns an internal `Cown`; a scope passed positionally is desugared into the acquire set, `scope.value` writes it, `__exit__` unwraps it. Isolated commit. |
| 11 | low | `finalize-pr` skill: version bump, CHANGELOG, Sphinx, README, `__init__.pyi`, editor-lens scrub, PR-gate mirror, public-C-ABI grep gate. |

### Open risks team reviewers might want to weigh in on

1. **D3 (alive-tracking mechanism).** We picked an atomic counter (yes/no answer to Q1). The alternative — a C-side intrusive registry — would buy debug observability ("list alive scopes") at the cost of a mutex on every scope create/release and a teardown story across sub-interpreter finalisation. The rebuttal round considered this carefully ([30-rebuttal-alive-tracking-mechanism-*.md](.copilot/plans/structured-concurrency/)). If you want introspection in v1, this is the decision to revisit.

2. **D4 (snapshots in this PR vs follow-up).** We chose to ship `stats=` and `noticeboard=` inside this PR, in two isolated commits (steps 8 and 9). The argument for deferral is that the wake-path snapshot and the noticeboard cycle are the two highest-blast-radius bits of the design; the argument for inclusion is that deferring means touching the public `Terminator`/`behaviors` constructor signatures twice. If snapshot capture turns up subtle issues during implementation, splitting is still cheap (each is one commit).

3. **Step 3 — `behavior_release_all_impl` as the single dec site.** This collapses three Python-side `terminator_dec` calls (worker.py, `stop()` drain, pump-bounded loop) into one canonical C dec at the tail of `behavior_release_all_impl`. The plan calls for a tight regression test (system count returns to baseline after every CM exit; sentinels carry `b->terminator == NULL`). If anyone sees a release-path edge that *doesn't* go through `release_all` today, please flag it before step 3 lands.

4. **Step 5 — `behavior_terminator_wrapper(b)` two-branch helper.** Because `_core_main_pump_bounded` has only a `BOCBehavior *` in scope (no `BehaviorCapsuleObject`), the helper takes a single `BOCBehavior *` and branches on `b->terminator == terminator_system()`: cached per-interpreter wrapper (borrowed ref) or fresh `Terminator._wrap()` (owned, decref'd after `Reset`). The free-threaded gate test for this step pins a user terminator into the pinned-pump queue manually. Refcount discipline in the bracket is the thing to double-check during code review.

5. **Q1 wording.** A user terminator that outlives `Behaviors.stop()` blocks the next `Behaviors.start()` with a count-only error message ("cannot start: N user Terminator(s) are still alive"). If anyone wants a richer diagnostic (e.g. "alive since file:line"), say so now — adding it later is purely additive but requires the registry from D3.

## How to read what follows

The rest of this document is **the implementer's plan** — 11 numbered steps with explicit file lists, verification gates, checkpoint discipline, and tests to write. Each step is intended to land as one commit (the project squash-merges PRs, so the within-PR history matters only for bisect during implementation).

Section structure (already below):

- **Design decisions (resolutions)** — D1–D8 plus four "gap" items pulled out of the analysis pass.
- **Plan** — steps 0 through 11. Each step lists *Goal*, *Files touched*, *Verification*, and *Checkpoint*.

If you only have ten minutes, read the design decisions and skim the *Goal* line of each step.

---

# Structured-concurrency PR — draft plan

## Design decisions (resolutions)

- **D1 (c-rewrite granularity): single-step in-place rewrite.** The
  speed-lens rebuttal is decisive on two grounds: (i) the project
  squash-merges PRs (`git log --oneline` shows one commit per PR
  including large C-subsystem PRs like `verona-rt`, `noticeboard +
  distributed scheduler`, `cross-module-behaviors`), so the
  conservative-lens "bisect window per intra-PR substep" benefit is
  not realised in the project's actual history; (ii) the conservative
  plan's intermediate states (transitional `_t`-suffixed forwarders +
  `#define` field aliases) are transitional scaffolding, not code
  anyone would ship, and reviewers would have to read and discard
  them. We adopt **the in-place rename**: every existing
  `terminator_*` C function takes `boc_terminator_t *t` as its first
  argument, and every in-tree C callsite passes `terminator_system()`
  in the same commit. The conservative lens's legitimate worry — that
  rc/parent lifecycle code is unique-to-this-PR and high-risk — is
  addressed by splitting that primitive into **its own step (step 2)**
  *before* any production caller depends on it, and by gating the
  internal accessors used to test it with `BOCPY_BUILD_INTERNAL_TESTS`
  (D7).

- **D2 (system storage): heap-allocated at `_core_module_exec`.**
  `BOCMutex` / `BOCCond` are project-internal abstractions whose
  static-initialisability across the six supported venvs (and across
  `.env313d` debug builds) is not asserted anywhere; the sketch's
  wording is heap-allocation at module exec. The cache-line savings
  speed-lens claims are real but unmeasured; we choose the portable
  option and revisit only if a benchmark gate fires. The Python
  wrapper for the system terminator is cached as a module-level
  PyObject so the hot dispatch path never reallocates it.

- **D3 (alive-tracking mechanism): atomic counter
  (`alive_children`) on the system terminator.** The conservative-lens
  rebuttal's §3 point 3 is decisive: a Python `WeakSet` agrees with
  the *Python wrapper* lifetime, not the *C struct* lifetime, so it
  produces a **silent Q1 false-negative** when a user drops the
  `Terminator` wrapper while behaviors are still in flight against
  it. The intrusive C list (speed-lens) avoids that false-negative but
  introduces a mutex on every scope create/release, a teardown
  story across sub-interpreter finalisation, and observability that
  is not actually accessible from Python without inventing a stable
  per-interpreter wrapper identity the design explicitly disclaims
  (per [00-context.md](./00-context.md) invariant 5). The counter is
  the only option that (i) agrees with the canonical C lifetime, (ii)
  has no teardown story, (iii) is yes/no observable to Q1, and (iv)
  is forward-compatible — adding a registry later is purely additive.
  The Q1 error message will report the count from the atomic. If real
  usage demands per-instance identification, a debug list can be
  added in a follow-up as an *additional* mechanism alongside the
  counter.

- **D4 (snapshots): include in this PR, in two isolated commits.**
  The usability-lens rebuttal is decisive on the public-API symmetry
  point — the sketch's Q5 *resolved* that snapshots live on the
  `Terminator` constructor, and shipping the CM without
  `scope.stats` / `scope.noticeboard` means the same public surface
  (`Terminator.__init__`, `behaviors.__init__`, `__init__.pyi`,
  Sphinx, README, CHANGELOG) gets touched twice. The
  conservative-lens bisect concern is addressed by **placing each
  snapshot wiring in its own step**: stats lands as one step (C wake
  path, gated by a relaxed-atomic flag read), noticeboard as a second
  step (Python wait-wrapper post-success cycle). Each step has its
  own test file. A snapshot regression bisects to a single intra-PR
  commit. Reasoning per
  [30-rebuttal-snapshots-mvp-or-follow-up-usability-lens.md](./30-rebuttal-snapshots-mvp-or-follow-up-usability-lens.md)
  §6 "name one defect class".

- **D5 (public C ABI handling): verified non-impacting.** `grep -nE
  'terminator|Terminator' src/bocpy/include/bocpy/*.h` returns **zero
  matches** today (the public ABI exposes `bocpy.h`, `xidata.h`, and
  the MSVC shim — none mentions terminators). Invariant for this PR:
  **no symbol in this work may enter
  `src/bocpy/include/bocpy/*.h`**. A grep check in the finalize step
  enforces it. No downstream consumer migration is required;
  `templates/c_abi_consumer/` and `test_public_c_abi.py` are not
  expected to need updates.

- **D6 (C function naming): in-place rename, no suffix.** Falls out
  of D1: the canonical signatures take `boc_terminator_t *t` as the
  first argument; the existing names (`terminator_inc`,
  `terminator_dec`, …) keep their identifiers. The Python entry points
  in `_core.c` (e.g. `_core_terminator_inc`) keep their existing
  Python-level signatures and internally pass `terminator_system()`.

- **D7 (internal-test helpers gating): `BOCPY_BUILD_INTERNAL_TESTS`.**
  Per [.github/copilot-instructions.md](../../../.github/copilot-instructions.md)
  §"Build and Test", any new `_core` accessor that exists solely for
  internal tests (`_core._system_terminator_id`,
  `_core._terminator_alive_user_count`, etc.) is gated under
  `BOCPY_BUILD_INTERNAL_TESTS` in [setup.py](../../../setup.py) and
  tests that need it use `pytest.importorskip`. The env var is
  already wired into `.github/workflows/pr_gate.yml`.

- **Single canonical C dec site for the worker path.** Per
  adversarial-iter-1 F1. The C function `behavior_release_all_impl`
  is extended in step 3 to call `terminator_dec(b->terminator)` at
  its tail, and the explicit `terminator_dec(terminator_system())`
  calls at `_core.c` lines 4519 and 4615 (the pinned-pump bounded
  loop and the stop-drain loop) are **removed** in the same commit
  so all three release paths share one dec site. Both Python sites
  that today match the schedule-time `terminator_inc` — `worker.py`
  line 67 and the drain loop in `behaviors.py` (~line 1538) — drop
  their `_core.terminator_dec()` calls in step 3. A regression
  test in step 3 schedules N behaviors through ordinary `@when`
  (not pinned) and asserts the system count returns to baseline
  after CM exit (would fail loudly on either double-dec or
  missed-dec). The rollback dec in `whencall` (`behaviors.py` line
  1881) stays in place in step 3 — at that point all behaviors
  still resolve to system, so the no-arg dec is correct — and is
  switched to dec the resolved terminator in step 6.

- **Pumpable wait legality on user terminators: raise.** The sketch
  states `terminator_wait_pumpable` is "only legal on the system
  terminator" because it talks to the global pinned-pump hook. Step 1
  encodes this as a guard in the C function: calling
  `terminator_wait_pumpable(t, …)` with `t != terminator_system()`
  raises `ValueError` from the Python entry point. Test added in
  step 4 (alongside the new `Terminator.wait()` test surface).

- **Module-unload teardown:** the system terminator is allocated once
  in `_core_module_exec` and never released. The kernel objects
  (mutex/cond) and the `alive_children` atomic outlive module
  unload — this matches the current header-comment contract in
  [boc_terminator.h](../../../src/bocpy/boc_terminator.h) ("kernel
  objects outlive module unload"). User terminators are released by
  their owners' `tp_dealloc` and by C-level capsule retain/release.
  No new teardown hook is added. A one-line note records this in the
  rewritten header docstring.

- **`_resolve_terminator` caching: cache `_current_terminator.get`
  at module scope.** Per speed-lens step 4 rationale: hoist the bound
  method (`_cv_get = _current_terminator.get`) so each `whencall`
  pays one bound-method call plus one `is`-test against a cached
  `_MISSING` sentinel and a fall-through to the cached system
  wrapper. No per-call attribute lookups on the ContextVar.

- **D8 (scope output value): a single `value` slot backed by one
  per-scope cown, desugared from a positional scope argument.** A
  scope carries an internal `Cown(None)`. Listing the scope among a
  behavior's cowns (`@when(a, b, scope)`) is desugared in `whencall`:
  the scope is detected **by type** (not by identity against the
  active-scope contextvar — a caller may legitimately pass a scope it
  was handed rather than the one it is currently inside) and replaced
  with `scope._output` in the acquire set. The body then receives the
  output cown as its `scope` parameter and writes `scope.value` under
  exclusive access, exactly like any other cown. `__exit__` waits for
  quiescence and moves the cown's final value onto `scope.value`
  (`None` if no behavior wrote it — the plain default, no `_UNSET`
  sentinel). Three sub-decisions, all resolved:
  - **Single slot, last-writer-wins.** Multiple writers serialise on
    the one cown, and that serialisation is precisely what makes
    `scope.value = scope.value + x` reductions race-free. Per-writer
    result *collection* is explicitly out of scope: it would force a
    worker scheduling a scope-writing sub-behavior to register a
    fresh cown back onto a scope living on another interpreter — a
    cross-interpreter registration problem the single shared cown
    sidesteps entirely.
  - **The scope arg drives the terminator; at most one per behavior.**
    Because the output write and the `__exit__` wait must target the
    same rundown counter (otherwise `wait()` can return before a
    worker's write commits and `scope.value` reads a half-written
    cown), a positional scope argument overrides the contextvar and
    binds this behavior's terminator to that scope. A behavior may
    list at most one scope; two scopes have no sensible terminator
    answer and raise `TypeError` at schedule time.
  - **Exceptions never touch the slot.** A behavior that raises does
    not write `scope.value`; its exception surfaces on that
    behavior's own result cown via the existing path. With several
    possible writers there is no coherent "which exception wins"
    answer, so the slot deliberately carries only successful writes.
  The output cown rides with the scope across interpreters the same
  way the terminator wrapper does (step 5): the per-interpreter scope
  wrapper is reconstructed around the same underlying cown capsule, so
  a worker-side `scope._output` resolves to the capsule the primary
  interpreter unwraps at `__exit__`.

## Plan

### Step 0 — Baseline

**Goal.** Pin a green test/lint baseline in the venv the user picks at
execution time; record pre-existing failures, skips, and xfails so
later regressions are unambiguous; verify the D5 public-ABI invariant.

**Actions.**
- Ask the user which venv to use (do **not** assume `.env314`).
  Default suggestion `.env314`; nominate one free-threaded venv
  (`.env313t` or `.env315t`) for step 5 and step 11.
- In the chosen venv: `pip install -e .[test,linting]`;
  `pytest -vv`; `flake8 src/ test/ examples/`. Record results
  verbatim into `.copilot/plans/structured-concurrency/baseline.md`.
- Run [examples/benchmark.py](../../../examples/benchmark.py) and
  [examples/fanout_benchmark.py](../../../examples/fanout_benchmark.py)
  in the chosen venv and the nominated free-threaded venv;
  paste the per-stage timings into `baseline.md` (per
  adversarial-iter-1 F15). Step 3 re-runs both benchmarks and
  diffs against this baseline.
- Read [.github/skills/testing-with-boc/SKILL.md](../../../.github/skills/testing-with-boc/SKILL.md)
  (mandatory per `/memories/repo/testing-with-boc-mandatory-read.md`)
  and [.github/skills/commenting-c-and-python/SKILL.md](../../../.github/skills/commenting-c-and-python/SKILL.md)
  before writing any code.
- `grep -nE 'terminator|Terminator' src/bocpy/include/bocpy/*.h` and
  confirm zero matches; record the result in the baseline file as
  the invariant the PR must preserve (D5).
- Write `.copilot/plans/structured-concurrency/50-progress.md` and
  keep it updated as steps complete (per
  `/memories/repo/multistep-plans-checkpoint-commits.md`).

**Verification.** Baseline run is green (or every failure is
documented as pre-existing). Plan file is on disk.

**Checkpoint.** **No commit** (read-only step).

---

### Step 1 — Promote `boc_terminator` to a heap object

**Goal.** Rewrite `boc_terminator.{c,h}` so the file-scope statics
become a heap-allocated `boc_terminator_t` struct, with every existing
function taking `boc_terminator_t *t` as its first argument. Allocate
the **system instance** once in `_core_module_exec`; every in-tree
callsite (C and Python entrypoints) passes `terminator_system()` so
**observable behaviour is unchanged**. Encode the
"`terminator_wait_pumpable` only on system" invariant as a runtime
guard.

**Files touched.**
- [src/bocpy/boc_terminator.h](../../../src/bocpy/boc_terminator.h) —
  declare `boc_terminator_t` (fields: `count`, `seeded`, `closed`,
  `rc`, `alive_children` (declared but not used until step 2),
  `mutex`, `cond`, `parent`), `terminator_system(void)` accessor.
  Rewrite every existing prototype to take `boc_terminator_t *t`
  first. Update the header docstring to describe the new lifecycle
  contract, the system-singleton invariant, and the
  "kernel-objects-outlive-module-unload" note.
- [src/bocpy/boc_terminator.c](../../../src/bocpy/boc_terminator.c) —
  implement the struct; expose a shared in-place initializer
  `terminator_init_inplace(boc_terminator_t *t, boc_terminator_t
  *parent)` (kernel-object init + `count=1` + `seeded=1` + `rc=1`
  + `parent` link, no alloc). The system instance is created by
  allocating once in `_core_module_exec` and calling
  `terminator_init_inplace(t, NULL)`. Step 2's `terminator_new`
  reuses the same initializer so the kernel-object init path is
  exercised by step-2 tests (per adversarial-iter-1 F5). Add the
  `wait_pumpable` guard: if `t != terminator_system()`, return an
  error code (the existing Python entry point
  `_core.terminator_wait_pumpable(timeout_s)` always passes the
  system pointer today, so this guard is defence-in-depth; no
  user-reachable surface exercises the failure branch, per
  adversarial-iter-1 F6 — record the invariant as a comment in
  `boc_terminator.c` instead of a test). Doxygen
  `@brief`/`@details`/`@param`/`@return` on every function per
  the commenting skill.
- [src/bocpy/_core.c](../../../src/bocpy/_core.c) — rewrite every
  in-file callsite (per `00-context.md`: ~lines 849, 859, 869, 888,
  929, 953, 982, 1027, 1037, 1905, 2295, 4519, 4615) to pass
  `terminator_system()`. The Python-level entry points
  (`_core_terminator_inc`, `_dec`, `_close`, `_wait`,
  `_seed_inc`/`_seed_dec`, `_seeded`, `_count`, `_wake_all`,
  `_wait_pumpable`, `_reset`) keep their Python-side signatures and
  internally pass `terminator_system()`. Add the
  `_core_module_exec` call to `terminator_system_init()`.
- Any other `.c` file that calls a `terminator_*` function (e.g.
  `boc_pump.c`, `boc_sched.c`) — grep and rewrite to pass
  `terminator_system()`.

**Verification.**
- `pip install -e .` rebuilds the extension.
- `pytest -vv` in the chosen venv — **must match the step-0
  baseline** (no new failures, no new skips beyond pre-existing).
  This is a pure refactor.
- Particular attention to `test_stop_retry_composition.py`,
  `test_scheduler_integration.py`,
  `test_scheduling_stress.py`,
  `test_public_c_abi.py`. If the wake-on-zero contract broke, these
  hang.
- `flake8 src/` clean.
- `grep -nE 'terminator|Terminator' src/bocpy/include/bocpy/*.h`
  still returns zero matches (D5 invariant preserved).

**Checkpoint.** Working tree clean; user commits with subject
`refactor(terminator): promote to heap object, system singleton, no behaviour change`.

---

### Step 2 — rc / parent / `alive_children` plumbing (off the production path)

**Goal.** Add `terminator_new(parent)`, `terminator_retain(t)`,
`terminator_release(t)`, and the `alive_children` atomic increment /
decrement on the system terminator. **No production caller invokes
these yet** — they exist behind internal-test accessors so the
primitive is unit-tested before step 3 wires it into the schedule path.

**Files touched.**
- [src/bocpy/boc_terminator.c](../../../src/bocpy/boc_terminator.c) —
  implement `terminator_new(parent)` (alloc + init mutex/cond +
  `count=1`, `seeded=1`, `rc=1` + on `parent == terminator_system()`,
  `atomic_fetch_add_explicit(&parent->alive_children, 1,
  memory_order_relaxed)` + `terminator_inc(parent)`),
  `terminator_retain(t)` (`++rc` relaxed),
  `terminator_release(t)` (`--rc` acq-rel; on 0 → close kernel
  objects, decrement `parent->alive_children` and `terminator_dec`
  the parent, `free`). Doxygen on each.
- [src/bocpy/_core.c](../../../src/bocpy/_core.c) — add internal-test
  helpers exposed only when `BOCPY_BUILD_INTERNAL_TESTS=1`:
  `_core._system_terminator_id()`,
  `_core._terminator_alive_user_count()`,
  `_core._behavior_terminator_id(capsule)` (returns
  `(uintptr_t)b->terminator` for the capsule, per adversarial-iter-1
  F9; consumed by step 3's round-trip test), plus thin Python
  wrappers `_core._terminator_new_test()`,
  `_core._terminator_retain_test()`, `_core._terminator_release_test()`.
  Mirror the `bocpy._internal_test` gating in
  [setup.py](../../../setup.py).
- [test/test_terminator_internals.py](../../../test/test_terminator_internals.py)
  (new) — use `pytest.importorskip("bocpy._internal_test")` (or the
  `_core` gated symbol) to:
  - Construct N terminators; observe `alive_user_count == N`; release
    them; observe `== 0`.
  - Retain/release a non-zero rc; observe count stays alive across
    intermediate releases; final release frees.
  - Construct two wrappers around the same C struct via
    `_terminator_retain_test`; release each; observe single
    `alive_children` decrement at the rc → 0 boundary.

**Verification.**
- `BOCPY_BUILD_INTERNAL_TESTS=1 pip install -e .[test]`.
- `pytest -vv test/test_terminator_internals.py` passes.
- Full `pytest -vv` matches step-1 baseline (no production caller is
  using the new primitives yet, so the suite should be unchanged).
- `flake8 src/ test/` clean.

**Checkpoint.** User commits with subject
`feat(terminator): rc/parent/alive-children primitives behind internal-test gate`.

---

### Step 3 — `BOCBehavior` carries `boc_terminator_t *terminator`; collapse dec sites

**Goal.** Every `BOCBehavior` gets a strong ref to its terminator at
construction (initially always `terminator_system()`); the schedule
path inc's `b->terminator`; **one canonical C dec site**
(`behavior_release_all_impl`) dec's `b->terminator`; `behavior_free`
releases the retain. All redundant Python-side dec calls in production
paths that today match the schedule-time inc are removed in the same
commit (per adversarial-iter-1 F1 + F4).

The new terminator parameter on `BehaviorCapsule_init` is **keyword-
only with `None` as default**, resolving to `terminator_system()`
when omitted, so the four existing in-tree direct constructions in
`test_boc.py:1122/1140` and `test_scheduling_stress.py:617/729`
continue to compile and run unchanged (per adversarial-iter-1 F3).
Step 6 tightens the parameter to **required** keyword-only once
`whencall` is the sole production caller.

**Files touched.**
- [src/bocpy/_core.c](../../../src/bocpy/_core.c):
  - Add `boc_terminator_t *terminator;` to `BOCBehavior` (~line 3309).
    One-line Doxygen `@brief`: *"Strong ref to this behavior's
    terminator (one retain at attach, one release at free)."*
  - `behavior_new` initialises `terminator = NULL`.
  - `BehaviorCapsule_init` (~line 3565): add `terminator=None` as a
    keyword-only argument. After the capsule is fully constructed,
    `b->terminator = terminator ? terminator->c : terminator_system();
    terminator_retain(b->terminator);`. (In step 3 the public Python
    wrapper does not exist yet; the kwarg is reachable from C-level
    callers only. Step 4 adds the wrapper; step 6 exposes the kwarg
    on `whencall`.)
  - `behavior_free`: `terminator_release(b->terminator)` if non-NULL.
  - Schedule path: leave the existing schedule-time `_core.terminator_inc()`
    call in [behaviors.py:1876](../../../src/bocpy/behaviors.py#L1876)
    untouched in step 3 — after step 1 the no-arg form internally
    passes `terminator_system()`, every behavior in step 3 still
    carries `b->terminator == terminator_system()`, and the
    schedule-time inc on system balances the new release-time dec on
    `b->terminator` (also system). Step 6 switches the
    schedule-time inc to dec the resolved terminator.
  - **One canonical dec site:** extend `behavior_release_all_impl`
    to call `terminator_dec(b->terminator)` at its tail. Then
    **remove** the two explicit `terminator_dec(terminator_system())`
    callsites at ~lines 4519 and 4615 (pinned-pump bounded loop and
    stop-drain loop) — they now go through `release_all` like the
    worker path. **Sentinels (`token_work`)**: their
    `b->terminator == NULL` (never set), and the release path
    early-returns on `is_token` before reaching the dec; assert
    `alive_children == 0` and system count unchanged across a
    sentinel-only run in the step-7 Q3 test.
  - `XIDATA_GETDATA_FUNC(_behavior_shared)` at ~line 4638: marshal
    `b->terminator` as a plain `void *` (pointer is durable across
    sub-interpreters because the C struct is process-global and
    its mutex/cond live in process memory; the Python wrapper is
    reconstructed per-interpreter in step 5). On the worker side,
    call `terminator_retain(b->terminator)` so the materialised
    capsule holds its own retain. `behavior_free` on the worker
    side then releases.
- [src/bocpy/worker.py](../../../src/bocpy/worker.py):
  - **Remove** the `_core.terminator_dec()` call in the outer
    `finally` of `run_behavior` (line 67). The canonical C dec site
    inside `behavior_release_all_impl` already handled it.
- [src/bocpy/behaviors.py](../../../src/bocpy/behaviors.py):
  - **Remove** the `_core.terminator_dec()` call in the
    `stop()` drain loop (~line 1538). Same reason — drain calls
    `payload.release_all()` which now dec's the capsule's
    terminator. The rollback dec in `whencall` (~line 1881) stays
    as the no-arg form in step 3 (all behaviors still resolve to
    system); step 6 switches it to dec the resolved terminator.
- [test/test_terminator_capsule.py](../../../test/test_terminator_capsule.py)
  (new) — use the step-2 accessors:
  - Schedule one `@when` through the ordinary scheduler (non-pinned
    cown); assert the system terminator's `count` returns to its
    `seeded` baseline after the behavior completes. Catches both
    double-dec and missed-dec.
  - Schedule N behaviors (mix of pinned and unpinned) and assert the
    same after `Behaviors.wait()`.
  - Schedule N behaviors and call `Behaviors.stop()` partway through
    so the drain path executes; assert system count returns to
    baseline.
  - Capsule round-trip: from within a `@when`, call
    `_core._behavior_terminator_id(<self_capsule>)` and assert it
    equals `_core._system_terminator_id()`.
- Re-run [examples/benchmark.py](../../../examples/benchmark.py)
  and [examples/fanout_benchmark.py](../../../examples/fanout_benchmark.py)
  in both the primary and the free-threaded venvs (per
  adversarial-iter-1 F15). Diff against `baseline.md`. If the diff
  shows a measurable regression on either venv, reorder the
  `BOCBehavior` struct so the new `terminator` pointer sits in the
  cold half (after `requests` / `requests_size`) and the hot
  `count` / `is_token` / `owner_worker_index` triple keeps its
  cache line.

**Verification.**
- Full `pytest -vv` matches step-2 baseline.
- New `test_terminator_capsule.py` passes.
- `test_scheduling_stress.py`, `test_scheduler_steal.py`,
  `test_stop_retry_composition.py` still pass — wake-on-zero must
  not have shifted.
- **Dec-site enumeration grep** (per adversarial-iter-2 A5):
  `grep -nE 'terminator_dec\(' src/ test/` returns exactly the
  expected post-step-3 set: the canonical C dec at the tail of
  `behavior_release_all_impl`, the rollback dec in
  `behaviors.py` (~line 1881, still no-arg in step 3), and any
  internal-test helpers from step 2. The four removals (worker.py:67,
  behaviors.py:1538, _core.c:4519, _core.c:4615) must not appear.
- `flake8 src/ test/` clean.

**Checkpoint.** User commits with subject
`refactor(behavior): BOCBehavior carries a terminator pointer; remove redundant Python-level dec`.

---

### Step 4 — Python `Terminator` type (construct / wait / count / _wrap)

**Goal.** Land the user-facing Python type, **without** `stats=` /
`noticeboard=` kwargs. Reserve those parameter names by accepting
keyword-only `stats: bool = False, noticeboard: bool = False` in
`__init__` and raising `NotImplementedError` if either is truthy —
this locks the signature so steps 8 and 9 are purely additive.

**Files touched.**
- [src/bocpy/_core.c](../../../src/bocpy/_core.c):
  - `Terminator` `PyTypeObject` registered on module exec.
  - `tp_init(*, stats=False, noticeboard=False)`: raise
    `NotImplementedError` if either kwarg is truthy; otherwise call
    `terminator_new(terminator_system())`.
  - `tp_dealloc`: `terminator_release(self->c)`.
  - Private classmethod `Terminator._wrap(uintptr_t c_ptr)`: call
    `terminator_retain(c_ptr)` and stash on a fresh wrapper. One-line
    docstring noting it is private (used only by capsule
    unmarshalling).
  - Methods: `wait(timeout=None)` (forwards to C `terminator_wait`,
    GIL-released, returns `bool`), `seed_dec()` (private; used by
    the CM in step 7), `_dec()` (private; pointer-taking dec used
    by `whencall`'s rollback in step 6 — per adversarial-iter-2
    A3), `count` read-only property, `__enter__` /
    `__exit__` (drops seed then `wait()`).
  - Error messages on `wait()` failure modes use distinct
    `RuntimeError` strings so users can distinguish (e.g. closed
    terminator vs propagated worker exception).
  - Cache the **system terminator's Python wrapper** as a module-level
    `PyObject` so step 5 can use it without per-dispatch allocation.
- [src/bocpy/__init__.py](../../../src/bocpy/__init__.py) — re-export
  `Terminator`.
- [src/bocpy/__init__.pyi](../../../src/bocpy/__init__.pyi) — stub
  with Sphinx-style docstring (full pass deferred to step 11's
  finalize, but the bare stub lives here from step 4 so step 5–9 do
  not break the stub file).
- [test/test_terminator.py](../../../test/test_terminator.py) (new) —
  follows
  [testing-with-boc](../../../.github/skills/testing-with-boc/SKILL.md):
  - `Terminator()` increments `alive_user_count`; `del t`/scope-exit
    decrements.
  - `Terminator(stats=True)` and `Terminator(noticeboard=True)` raise
    `NotImplementedError` (reserved-but-not-implemented). The kwarg
    signature is the **exact** one step 8 (stats) and step 9
    (noticeboard) will deliver — only the body's
    `NotImplementedError` is lifted in those steps (per
    adversarial-iter-1 F12).
  - `_wrap` round-trip: construct on primary interpreter; via
    `BehaviorCapsule` ship to a worker; assert worker-side wrapper
    references the same C pointer (use the
    `_core._system_terminator_id()`-style accessor extended for any
    `Terminator`).
  - **Alive-terminator pins runtime**: schedule a behavior into a
    `Terminator` `t`; do **not** drop `t`; call
    `bocpy.wait(timeout=0.5)`; assert it raises / returns False
    (the system terminator's count is held positive by `t`'s system
    hold).

**Verification.**
- `pytest -vv test/test_terminator.py` passes.
- Full `pytest -vv` matches step-3 baseline.
- `flake8 src/ test/` clean.

**Checkpoint.** User commits with subject
`feat(terminator): first-class Python Terminator type (no snapshots yet)`.

---

### Step 5 — ContextVar plumbing in C `execute()` (default-to-system)

**Goal.** Declare `_current_terminator: ContextVar[Terminator]` and
the `_resolve_terminator(explicit)` helper; install the worker-side
`PyContextVar_Set` / `PyContextVar_Reset` bracket around the thunk
call in C `execute()` using `b->terminator`'s wrapper as the value.
Every behavior still carries the system terminator, so the
observable surface is unchanged — but the propagation machinery is
now live and testable. **This is the highest-risk concurrency change
in the PR; it runs in `.env314` and one free-threaded venv before
proceeding.**

**Files touched.**
- [src/bocpy/_core.c](../../../src/bocpy/_core.c):
  - At `_core_module_exec`, cache `_current_terminator` as a
    module-level static `PyObject *S_CURRENT_TERMINATOR_CV` by
    importing it from `bocpy.behaviors` (no per-dispatch import).
  - Cache the system terminator's Python wrapper (already set up in
    step 4).
  - **Cached `Terminator` wrapper lives on `BehaviorCapsuleObject`
    (per-interpreter), not on `BOCBehavior` (process-wide)** (per
    adversarial-iter-1 F8). The wrapper is constructed lazily on
    each interpreter that materialises the capsule (either in
    `_new_behavior_object` on the worker side or eagerly in
    `BehaviorCapsule_init` on the producer side) and `Py_DECREF`'d
    in the capsule's `tp_dealloc`. The **only** process-wide
    cached wrapper is the per-interpreter system-terminator wrapper
    allocated at `_core_module_exec` and freed at module unload
    (matches per-interpreter lifetime exactly). For user terminators,
    every interpreter that materialises the capsule builds its own
    wrapper from `b->terminator` via `Terminator._wrap()`, which
    retains the C struct exactly once.
  - **`behavior_terminator_wrapper(BOCBehavior *b)` is the single
    helper called by the contextvar bracket** (per
    adversarial-iter-2 A1). It has only a `BOCBehavior *` in scope
    because the pinned-pump caller at `_core_main_pump_bounded`
    (line 4493) has no `BehaviorCapsuleObject`. Two-branch logic:

    ```c
    static PyObject *
    behavior_terminator_wrapper(BOCBehavior *b)
    {
        if (b->terminator == terminator_system()) {
            return S_SYSTEM_TERMINATOR_WRAPPER; /* per-interpreter cache, borrowed */
        }
        return Terminator_wrap(b->terminator);  /* fresh wrapper; caller decref's after Reset */
    }
    ```

    The `BehaviorCapsuleObject`-side cache is **optional
    optimisation only** for the worker path; the bracket always
    calls the helper above and the bracket itself decref's the
    returned wrapper if it is a fresh (non-cached) one.
  - Around `PyObject_Call(thunk, …)` at ~line 4185:

    ```c
    PyObject *token = PyContextVar_Set(S_CURRENT_TERMINATOR_CV,
                                       behavior_terminator_wrapper(b));
    if (token == NULL) { /* set error path */ }
    result = PyObject_Call(thunk, thunk_args, NULL);
    PyContextVar_Reset(S_CURRENT_TERMINATOR_CV, token);
    Py_DECREF(token);
    ```
- [src/bocpy/behaviors.py](../../../src/bocpy/behaviors.py):
  - Module-level: `_current_terminator: ContextVar[Terminator] =
    ContextVar("bocpy.current_terminator")` (no default — the helper
    falls back to system on `LookupError`).
  - Hoist `_cv_get = _current_terminator.get` and `_cv_MISSING =
    object()` for the cached fast-path in step 6's `whencall`.
  - `_resolve_terminator(explicit)` helper: if `explicit is not None
    return explicit`; else `t = _cv_get(_cv_MISSING)`; if
    `t is _cv_MISSING return _system_terminator`; else return `t`.
  - `_system_terminator` is the cached Python wrapper exported by
    `_core`.
- [test/test_terminator_propagation.py](../../../test/test_terminator_propagation.py)
  (new):
  - Add a `_core.current_terminator_id()` internal-test accessor
    (gated by `BOCPY_BUILD_INTERNAL_TESTS`) that returns the
    address of `_current_terminator.get(MISSING)` — or the system
    pointer if MISSING.
  - Inside a `@when` body, assert `current_terminator_id()` equals
    the capsule's terminator (still the system one at this step).
  - Behavior A inside a `@when` schedules behavior B with no
    explicit terminator; assert B observes the same terminator id
    as A (transitive propagation works even when everyone resolves
    to system — this is the wiring test).
  - **Pinned-pump user-terminator body** (per adversarial-iter-2 A1):
    use the internal-test accessor (or a step-2 forwarder) to
    construct a user terminator `t`; manually attach a `BOCBehavior`
    with `b->terminator = t` to the pinned queue and invoke
    `_core_main_pump_bounded` once; assert the body observes
    `current_terminator_id() == id(t)`. This is the regression test
    for the two-branch helper above. (The public path for this
    test — a user-terminator pinned `@when` inside a `with
    behaviors() as t:` scope — is not yet reachable in step 5
    because the `terminator=` kwarg lands in step 6; step 7's
    pinned-pump Q3 sub-assert exercises the public path once it
    is available.)

**Verification.**
- `pytest -vv test/test_terminator_propagation.py` passes.
- Full `pytest -vv` matches step-4 baseline.
- **Re-run the full suite in the free-threaded venv** the user
  nominated in step 0 (`.env313t` or `.env315t`). A
  `PyContextVar_Set` race surfaces here first because the per-thread
  contextvar storage is the most likely source of a free-threaded
  hazard.
- `flake8 src/ test/` clean.

**Checkpoint.** User commits with subject
`feat(terminator): worker-side contextvar propagation (default-to-system)`.

---

### Step 6 — `terminator=` kwarg on `whencall` and `@when`; reserved-name collision check

**Goal.** Make `whencall` and `@when` accept an explicit
`terminator=` kwarg; route through `_resolve_terminator`; pass the
resolved terminator into `BehaviorCapsule_init`. Reserve the bare
parameter name `terminator` and raise a precise `TypeError` at
decoration time if a user behavior declares a parameter named
`terminator`. **This is the first commit at which non-system
terminators actually flow through the schedule path.**

**Files touched.**
- [src/bocpy/behaviors.py](../../../src/bocpy/behaviors.py):
  - `whencall(func, args, captures, *, terminator: Terminator | None = None) -> Cown`
    — call `_resolve_terminator(terminator)`, pass the result to
    `BehaviorCapsule`, take the inc on the resolved terminator with
    the existing rollback on failure.
  - `when(*cowns, terminator: Terminator | None = None)` decorator —
    at decoration time inspect `inspect.signature(func).parameters`
    (per adversarial-iter-1 F13 — covers positional, positional-or-
    keyword, keyword-only, var-positional, and var-keyword names);
    if any parameter is named `"terminator"`, raise `TypeError`
    with a precise message naming the function (`func.__qualname__`)
    and the suggested rename (`terminator_`). One-line docstring
    change documenting the reserved name.
  - **Switch the rollback dec in `whencall` (~line 1881) to dec the
    resolved terminator** (per adversarial-iter-1 F4 +
    adversarial-iter-2 A3): the rollback path now calls
    `resolved._dec()` (the private Python method added in step 4)
    instead of the no-arg `_core.terminator_dec()`. Adds a test that
    simulates a `behavior_schedule` failure on a user terminator
    and asserts the user-terminator count returns to baseline.
- [src/bocpy/_core.c](../../../src/bocpy/_core.c) —
  `BehaviorCapsule_init` already accepts the terminator from step 3
  as keyword-only with `None` default; that signature is **kept**
  unchanged in step 6 (per adversarial-iter-2 A2). `whencall` is the
  only production caller and always passes the resolved terminator
  explicitly; the four legacy direct constructions in
  `test_boc.py:1122/1140` and `test_scheduling_stress.py:617/729`
  continue to compile and run using the `None` default. No in-tree
  test sites need updates.
- [src/bocpy/__init__.pyi](../../../src/bocpy/__init__.pyi) — update
  `when` / `whencall` signatures.
- [test/test_terminator_resolution.py](../../../test/test_terminator_resolution.py)
  (new):
  - Explicit `terminator=t` overrides any ambient contextvar.
  - Ambient contextvar (set manually) is inherited by `whencall`
    when no explicit kwarg is provided.
  - Neither set → resolves to system.
  - **Reserved-name collision tests** (per adversarial-iter-1 F13):
    `@when(c)` on each parameter kind — `def behavior(c, terminator)`
    (positional-or-keyword), `def behavior(c, *, terminator)`
    (keyword-only), `def behavior(c, *terminator)` (var-positional),
    `def behavior(c, **terminator)` (var-keyword) — each raises
    `TypeError`. Assert the message contains both the function name
    and the word `terminator`.
  - **Cross-worker propagation under a real user terminator**:
    construct `t`, inside `@when(c, terminator=t)` schedule an inner
    `@when(c2)` with no kwarg; assert the inner runs against `t`
    (uses the internal accessor from step 5). This is the test
    that ratifies Q4 + step 5 + step 6.

**Verification.**
- `pytest -vv test/test_terminator_resolution.py` passes.
- Full `pytest -vv` matches step-5 baseline.
- `flake8 src/ test/` clean.

**Checkpoint.** User commits with subject
`feat(when): terminator= kwarg + ContextVar resolution + reserved-name collision check`.

---

### Step 7 — `behaviors()` context manager + Q1 + Q2

**Goal.** Land the user-facing CM. Wire Q1 (alive-user-terminator
blocks `Behaviors.start()`) and Q2 (`Behaviors.wait()` inside a CM
raises). The CM accepts `timeout=` only at this step; `stats=` and
`noticeboard=` are reserved kwargs that raise `NotImplementedError`
(matching the `Terminator` constructor from step 4).

**Files touched.**
- [src/bocpy/behaviors.py](../../../src/bocpy/behaviors.py):
  - `class behaviors:` with `__init__(*, stats=False, noticeboard=False, timeout=None)`
    (the `stats`/`noticeboard` args raise `NotImplementedError` if
    truthy; this locks the CM signature too). `__enter__` constructs
    the `Terminator`, sets the contextvar via `set`, returns the
    terminator. `__exit__` resets the token, calls `seed_dec()` on
    the terminator, then `wait(self._timeout)`. The drain is
    unconditional (runs even on exception) per the sketch.
  - **Q1 wiring at `Behaviors.start()`** (~line 1120): before
    `terminator_reset`, read
    `_core._terminator_alive_user_count()`; if `> 0`, raise
    `RuntimeError("cannot start: N user Terminator(s) are still
    alive")` with `N` substituted.
  - **Q2 wiring at all three real blocking entry points** (per
    adversarial-iter-1 F2 — there is no `Behaviors.wait()` method;
    the user-visible blocking surface is the module-level
    `bocpy.wait()`, the module-level `bocpy.quiesce()`, and the
    instance method `Behaviors.quiesce()`). Factor the check into
    a shared private helper `_assert_not_inside_scope()`:

    ```python
    def _assert_not_inside_scope() -> None:
        t = _cv_get(_cv_MISSING)
        if t is _cv_MISSING or t is _system_terminator:
            return
        raise RuntimeError(
            "called inside a behaviors() scope would deadlock; "
            "exit the scope or call scope.wait()"
        )
    ```

    Call it at the head of each of the three entry points.
- [src/bocpy/__init__.py](../../../src/bocpy/__init__.py) — re-export
  `behaviors`.
- [src/bocpy/__init__.pyi](../../../src/bocpy/__init__.pyi) — stub.
- [test/test_behaviors_scope.py](../../../test/test_behaviors_scope.py)
  (new) — follows
  [testing-with-boc](../../../.github/skills/testing-with-boc/SKILL.md)
  and [testing-message-queue](../../../.github/skills/testing-message-queue/SKILL.md):
  - Empty `with behaviors():` exits immediately.
  - `with behaviors():` schedules N behaviors; exit blocks until all
    quiesce; on exit, system count returns to baseline.
  - **Transitive across worker hops**: outer schedules A; A
    schedules B (no kwarg); B schedules C; outer CM exit waits for
    all three.
  - Nested scopes: outer + inner each only wait for their own work.
  - Two concurrent threads each enter their own `with behaviors():`;
    each waits only for its own scope.
  - **Q1**: hold a `Terminator()` in a local; call
    `Behaviors.start()`; assert `RuntimeError` whose message
    includes the alive count.
  - **Q2**: from inside a `with behaviors():`, call each of
    `bocpy.wait()`, `bocpy.quiesce()`, and `BEHAVIORS.quiesce()`;
    each raises `RuntimeError` whose message names the deadlock
    (per adversarial-iter-1 F2 — three real entry points, one
    shared helper).
  - **D3 receive/after containment**: inside `with behaviors():`,
    call `receive(tag, timeout=…, after=cb)` where `cb` schedules
    inner work; on block exit assert the inner work completed
    (uses `send`/`receive` per the message-queue testing skill).
  - **Exception path**: raise inside `with behaviors():`; assert
    the block still drains and re-raises the original exception.
  - **Timeout path**: `behaviors(timeout=0.01)` against a slow
    scheduled behavior; assert the CM raises `TimeoutError` and
    that the seed was dropped (system count returns to baseline once
    the in-flight behavior completes).
  - **Q3 internal-runtime threads stay on system** (rewritten per
    adversarial-iter-1 F7 — the original framing was unreachable
    because Q1 blocks `start()` from inside a scope). Three concrete
    asserts:
    1. With a `with behaviors():` active in thread T1, in thread T2
       call `_core.current_terminator_id()` from a context that
       runs on the noticeboard mutator thread (e.g. via a
       diagnostic notice-update callback). Assert the value equals
       `_core._system_terminator_id()`.
    2. Schedule a sequence of pinned behaviors interleaved with
       sentinel ticks. Between pinned-behavior bodies, the pump
       thread's `_current_terminator.get(MISSING)` must be MISSING
       (i.e. no contextvar residue from the previous body — the C
       `PyContextVar_Set`/`Reset` bracket cleaned up correctly).
    3. **Sentinel `token_work` invariants**: in a scheduler run
       that processes only `token_work` sentinels (no real
       behaviors), assert `_terminator_alive_user_count() == 0`
       throughout, and that the system terminator's `count` is
       unchanged before/after (sentinels never inc/dec).

**Verification.**
- `pytest -vv test/test_behaviors_scope.py` passes.
- `test_stop_retry_composition.py` still passes (Q1 augmented but
  doesn't break drift detection).
- Full `pytest -vv` matches step-6 baseline.
- Re-run in the free-threaded venv.
- `flake8 src/ test/` clean.

**Checkpoint.** User commits with subject
`feat(behaviors): behaviors() context manager + Q1 alive-blocks-start + Q2 wait-inside-scope guard`.

---

### Step 8 — `Terminator(stats=True)`: C wake-path stats snapshot (isolated commit)

**Goal.** Wire the `stats=True` kwarg from `Terminator.__init__` and
`behaviors(stats=)`. Baseline snapshot at construction; final
snapshot in C on the `count → 0` transition under the existing
broadcast mutex. Expose `t.stats` as the delta. **This is its own
commit so a snapshot regression bisects here.**

**Files touched.**
- [src/bocpy/boc_terminator.h](../../../src/bocpy/boc_terminator.h)
  / [.c](../../../src/bocpy/boc_terminator.c):
  - Add `atomic_int_least64_t stats_requested;` and a
    `boc_sched_stats_t stats_baseline; boc_sched_stats_t stats_final;`
    pair (or per-worker arrays sized at `WORKER_COUNT`) to the
    struct's cold half.
  - In `terminator_dec`, immediately before the `count == 0`
    `pthread_cond_broadcast`, do an
    `atomic_load_explicit(&t->stats_requested, memory_order_relaxed)`;
    if non-zero, call `boc_sched_stats_snapshot_all(&t->stats_final)`
    (extended from existing `boc_sched_stats_snapshot`) — under the
    mutex we already hold, allocation-free.
- [src/bocpy/_core.c](../../../src/bocpy/_core.c):
  - Drop the `NotImplementedError` for `stats=True` in
    `Terminator.__init__`. The constructor signature is **unchanged**
    from step 4 (per adversarial-iter-1 F12); only the body lifts
    the `NotImplementedError` and instead sets `stats_requested = 1`
    + snapshots `stats_baseline` from the constructing thread.
  - Add the `stats` property: raise `RuntimeError` with a precise
    message *("Terminator.stats requires stats=True and a successful
    wait()")* — split into two distinct messages so the user can
    distinguish "didn't request" from "wait hasn't succeeded yet".
- [src/bocpy/behaviors.py](../../../src/bocpy/behaviors.py):
  - Drop the `NotImplementedError` for `stats=True` in
    `behaviors.__init__`. Forward to `Terminator`. Add `cm.stats`
    property that forwards to the underlying terminator.
- [src/bocpy/__init__.pyi](../../../src/bocpy/__init__.pyi) — update.
- [test/test_terminator_stats.py](../../../test/test_terminator_stats.py)
  (new):
  - `with behaviors(stats=True)` over N scheduled behaviors; assert
    `scope.stats` delta is non-negative and sums to a sensible
    number of dispatches.
  - `stats=False` → reading raises; pre-`wait()` reading raises;
    distinct exception messages between the two cases.
  - Concurrent scopes each get their own delta.
  - Caveat documented in the sketch — two concurrent scopes will
    see each other's work in their deltas — is asserted as
    expected behaviour (so a future move to per-terminator-tagged
    counters does not regress without notice).

**Verification.**
- `pytest -vv test/test_terminator_stats.py` passes.
- `test_scheduler_stats.py` still passes — the existing snapshot
  call site is unchanged.
- Full `pytest -vv` matches step-7 baseline (apart from the new test
  file).
- Free-threaded run: this is the second-most-likely place for a
  free-threaded race (the new flag-load on the wake path) — re-run
  in the free-threaded venv.
- `flake8 src/ test/` clean.

**Checkpoint.** User commits with subject
`feat(terminator): stats= snapshot via C wake path`.

---

### Step 9 — `Terminator(noticeboard=True)`: waiter-thread noticeboard cycle (isolated commit)

**Goal.** Wire the `noticeboard=True` kwarg. On `Terminator.wait()`
returning `True`, if `noticeboard_requested`, the Python wrapper
calls `Behaviors.cycle_noticeboard()` on the waiter's thread and
stashes the dict on `self`. **This is its own commit so a
noticeboard-cycle regression bisects here.**

**Files touched.**
- [src/bocpy/_core.c](../../../src/bocpy/_core.c):
  - Drop the `NotImplementedError` for `noticeboard=True`. The
    constructor signature is unchanged from step 4 (per
    adversarial-iter-1 F12); add `noticeboard_requested` flag and a
    slot for the captured dict.
  - Add the `noticeboard` property with distinct error messages
    (same pattern as `stats`).
- [src/bocpy/behaviors.py](../../../src/bocpy/behaviors.py):
  - Drop the CM's `NotImplementedError` for `noticeboard=True`.
  - Wrap `Terminator.wait()` in Python: after the C `wait()`
    returns `True` and if `t.noticeboard_requested`, decide where
    to capture from. **Guarded path** (per adversarial-iter-1 F10
    + F11):
    - If `BEHAVIORS is None`, or `BEHAVIORS.noticeboard is None`,
      or `not BEHAVIORS.noticeboard.is_alive()`, or **not**
      `_core.is_primary()` (caller is on a worker sub-interpreter),
      fall back to `_core.noticeboard_snapshot()` directly — the
      same quick path `cycle_noticeboard()` itself uses when the
      mutator thread is unavailable. Stash that dict.
    - Otherwise, call `Behaviors.cycle_noticeboard()` (existing
      primitive at
      [line 1204-1234](../../../src/bocpy/behaviors.py#L1204))
      and stash its return.
    - `cycle_noticeboard()` calls `noticeboard_cache_clear()` per
      Invariant 3, so the waiter's per-thread cache is correctly
      invalidated on the primary-interpreter path; the fallback
      uses `noticeboard_snapshot()` which does not clear the cache
      but is read-only anyway (no subsequent behavior is scheduled
      from a worker post-wait).
  - Add `cm.noticeboard` property forwarding to the terminator.
- [src/bocpy/__init__.pyi](../../../src/bocpy/__init__.pyi) — update.
- [test/test_terminator_noticeboard.py](../../../test/test_terminator_noticeboard.py)
  (new):
  - `with behaviors(noticeboard=True)` over a behavior that writes
    a notice; assert `scope.noticeboard` contains the written key.
  - **Cache integrity (Invariant 3)**: from the waiter thread, after
    the scope exits, call `noticeboard()` (the per-behavior
    snapshot) and assert it is **not** frozen at the snapshot value;
    schedule a follow-up behavior that reads `noticeboard()` and
    assert it sees post-cycle state.
  - Reading `noticeboard` with `noticeboard=False` raises; reading
    pre-`wait` raises; distinct messages.
  - Concurrent scopes — both see noticeboard mutations that landed
    in their cycle windows (the sketch's documented caveat).
  - **Worker-side wait fallback** (per adversarial-iter-1 F10):
    inside a `@when` body (worker sub-interpreter), enter a nested
    `with behaviors(noticeboard=True) as inner:` and assert
    `inner.noticeboard` is populated via the
    `_core.noticeboard_snapshot()` fallback (not via
    `cycle_noticeboard`).
  - **Post-stop snapshot resilience** (per adversarial-iter-1 F11):
    construct a `Terminator(noticeboard=True)` after `stop()` has
    torn down the runtime; assert `t.noticeboard` returns the
    last-known snapshot rather than raising.
- Existing [test_noticeboard.py](../../../test/test_noticeboard.py)
  must still pass — `cycle_noticeboard()` semantics are unchanged.

**Verification.**
- `pytest -vv test/test_terminator_noticeboard.py` passes.
- `test_noticeboard.py` and `test_behaviors_scope.py` still pass.
- Full `pytest -vv` matches step-8 baseline (apart from new test).
- `flake8 src/ test/` clean.

**Checkpoint.** User commits with subject
`feat(terminator): noticeboard= snapshot via waiter-thread cycle`.

---

### Step 10 — Scope output value (`scope.value` via an internal cown)

**Goal.** Give each `behaviors()` scope / `Terminator` a single output
slot, `scope.value`, backed by a per-scope internal `Cown(None)`. A
scope listed among a behavior's cowns (`@when(a, b, scope)`) is
desugared so the behavior acquires the scope's output cown and binds
its terminator to that scope; the body writes `scope.value` under
exclusive access; `__exit__` moves the cown's final value onto
`scope.value`. Isolated commit so an output-slot regression bisects
here. Resolves D8.

**Files touched.**
- [src/bocpy/behaviors.py](../../../src/bocpy/behaviors.py):
  - The `Terminator` / `behaviors` scope gains a private
    `_output: Cown` initialised to `Cown(None)` at construction and a
    public `value` attribute initialised to `None`.
  - `whencall` cown-collection loop (~line 1855): before the
    `isinstance(item, (Cown, _core.CownCapsule))` test, detect a scope
    argument **by type** (`isinstance(item, Terminator)` / the scope
    type). On match, substitute `item._output` into the acquire set
    and record the scope as this behavior's terminator (overriding the
    `_resolve_terminator` result). Raise `TypeError` — naming the
    behavior — if more than one scope appears in the arg list.
  - `Terminator.__exit__` / `behaviors.__exit__`: after `wait()`
    returns `True`, set `self.value = <unwrap>(self._output)` via the
    same result-reading path `@when` uses for its result cown. A
    raising writer's exception is **not** surfaced here — per D8 the
    slot carries only successful writes; the exception stays on that
    writer's own result cown.
  - The scope wrapper reconstructed on a worker (step 5's
    per-interpreter wrapper) must expose the same `_output` cown
    capsule so a worker-side scope arg resolves to the primary
    interpreter's slot.
- [src/bocpy/__init__.pyi](../../../src/bocpy/__init__.pyi) — add the
  `value` attribute and document the positional-scope form of `when` /
  `whencall` (full Sphinx pass in step 11).
- [test/test_scope_output.py](../../../test/test_scope_output.py)
  (new) — follows
  [testing-with-boc](../../../.github/skills/testing-with-boc/SKILL.md):
  - `with behaviors() as scope:` schedules one `@when(a, scope)` that
    sets `scope.value`; after exit `scope.value` holds the written
    value.
  - No writer → `scope.value is None` after exit.
  - **Reduction**: N behaviors each do
    `scope.value = (scope.value or 0) + k`; after exit the sum is
    correct (the shared cown serialises the read-modify-writes).
  - **Transitive / cross-worker**: an outer scope-writing behavior
    schedules an inner `@when(c2, scope)` (scope obtained from the
    active-scope contextvar or captured); the inner write is visible on
    `scope.value` after the outer CM exits — proves the output cown and
    the terminator target the same scope.
  - **Two-scope arg**: `@when(a, scope1, scope2)` raises `TypeError` at
    schedule time.
  - **Raising writer**: a behavior that lists the scope and raises
    leaves `scope.value` unchanged (`None`) and surfaces the exception
    on its own result cown (assert via the result Cown's exception, per
    the testing skill).
  - **Terminator-binding correctness**: schedule `@when(a, scope)` for
    a scope while *not* inside its `with` block; assert the CM's
    `wait()` still waits for that behavior (the positional scope arg
    bound the terminator, not the ambient contextvar).

**Verification.**
- `pytest -vv test/test_scope_output.py` passes.
- Full `pytest -vv` matches step-9 baseline (apart from the new test).
- `test_behaviors_scope.py` and `test_terminator_resolution.py` still
  pass — terminator resolution now honours a positional scope arg but
  the contextvar default path is unchanged.
- Re-run in the free-threaded venv.
- `flake8 src/ test/` clean.

**Checkpoint.** User commits with subject
`feat(behaviors): scope.value output slot via per-scope cown`.

---

### Step 11 — Finalize-pr (docs + version + CHANGELOG + editor-lens + PR-gate mirror)

**Goal.** Run the
[finalize-pr](../../../.github/skills/finalize-pr/SKILL.md) skill end
to end: version bump across all required files, CHANGELOG entry,
Sphinx + README updates, `__init__.pyi` final pass, editor-lens scrub
of any review-process comment debt accumulated across steps 1–10, and
the local PR-gate lint + test mirror.

**Deliverables.**
- Version bump per the skill's enumeration.
- `CHANGELOG.md` entry under the new version describing
  `behaviors()`, `Terminator` (with `stats=`/`noticeboard=`), the
  `terminator=` kwarg on `@when` / `whencall`, Q1 (alive-blocks-
  start), Q2 (wait-inside-scope-raises), the `scope.value` output slot
  (D8 — positional scope arg, single last-writer-wins slot), and the
  resolved Q3/Q4/Q5 invariants. **Explicit caveat block** for
  `Terminator.stats` and `Terminator.noticeboard` carrying the
  "process-wide-at-a-well-defined-moment" wording verbatim from
  the sketch (per adversarial-iter-1 F14).
- `sphinx/source/api.rst`: document `bocpy.behaviors`,
  `bocpy.Terminator`, the resolution chain, the `scope.value` output
  slot and its positional-scope form, and the snapshot caveats
  (process-wide-at-a-well-defined-moment).
- `sphinx/source/index.rst` (or a new narrative file): a
  "Structured concurrency" section with a `with behaviors():`
  example, a `scope.value` output example, the propagation guarantee,
  and the Q1 / Q2 caveats stated plainly.
- `README.md`: short subsection with a `with behaviors():` example
  (including a `scope.value` line).
- `src/bocpy/__init__.pyi`: full Sphinx-style docstrings on
  `behaviors`, `Terminator`, the new `terminator=` kwarg, the
  `scope.value` output slot and positional-scope form, and the
  reserved-name caveat. **`Terminator.stats` and `Terminator.noticeboard`
  docstrings include the process-wide caveat verbatim** (per
  adversarial-iter-1 F14). Match the
  [commenting skill](../../../.github/skills/commenting-c-and-python/SKILL.md)
  conventions (D205/D209/Q000/N802 clean).
- **Skill audit** (per adversarial-iter-1 F16): `grep -n
  'BOCBehavior\|terminator'
  .github/skills/c-extensions-with-bocpy/SKILL.md`. If the skill
  mentions `BOCBehavior` field layout, add a paragraph documenting
  the new `terminator` pointer; otherwise no update.
- Run the **editor-lens** agent over the diff to scrub any TODOs,
  plan-tracking comments, or scratch explanations that crept into
  the source tree across steps 1–10.
- Run the local PR-gate mirror (per the `finalize-pr` skill): `flake8
  src/ test/ examples/` and the full `pytest` suite in **both** the
  primary venv chosen in step 0 **and** the free-threaded venv used
  in step 5.
- Final D5 grep check: `grep -nE 'terminator|Terminator'
  src/bocpy/include/bocpy/*.h` still returns zero matches.

**Verification.**
- `flake8` clean.
- Full `pytest -vv` clean in both the primary and free-threaded
  venvs the user named.
- `sphinx-build` clean (no warnings about missing references).
- Editor-lens pass produces no remaining comment debt.
- D5 grep returns zero matches.

**Checkpoint.** Per `/memories/preferences.md` the user makes the
final commit; the skill drives the bump but does not commit.
