"""Integration tests for the per-worker scheduler.

The unit-level coverage of the dispatch / pop / park primitives lives
in ``test_scheduler_pertask_queue.py`` and exercises the C API
directly via ``_core``. This file covers behaviours that can only be
validated end-to-end through the public ``@when`` surface:

- **TLS coverage**: under producer-local dispatch every registered
  worker must reach the ``pending``-eviction path at least once,
  proving every TLS slot is reachable from the running worker.
- **Cross-worker wake**: when one worker is busy and another parks,
  a fresh dispatch from the main thread must wake the parked worker
  so the new behaviour runs concurrently with the busy one.
- **Runtime re-entry**: ``start()`` / ``wait()`` / ``start()`` must
  complete two independent workloads without leaks.
- **Paired-release contract**: an uncaught exception inside an
  ``@when`` body must still release the cown so a follow-on
  ``@when`` on the same cown is scheduled and runs.
- **Parked-peer CPU criterion**: with W=2 and all work pinned to
  worker 0 via producer-locality, the process CPU/wall ratio over a
  bounded window must stay below ~1.5 — i.e. worker 1 actually
  sleeps in ``cnd_wait`` instead of spinning.

All tests use module-level classes/helpers (workers run in
sub-interpreters and import the test module to resolve symbols).
"""

import time

import pytest

import bocpy
from bocpy import _core
from bocpy import Cown, drain, receive, send, TIMEOUT, wait, when


RECEIVE_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Module-level helpers (must be importable by worker sub-interpreters)
# ---------------------------------------------------------------------------


class _Counter:
    """Plain counter used as cown payload in chain workloads."""

    __slots__ = ("count",)

    def __init__(self):
        """Initialise the counter at zero."""
        self.count = 0


def _ensure_quiesced():
    """Tear down any prior runtime so the test starts from a clean state.

    ``bocpy.wait()`` is a no-op when ``BEHAVIORS`` is ``None``; if a
    previous test left the runtime up it drains and stops it.
    """
    bocpy.wait()


def _coverage_done(c_pin, marker):
    """Final ``@when`` extracted to a helper so the transpiler captures cleanly.

    Inlining this inside ``_coverage_kickoff`` triggers a transpiler
    capture-resolution gap: the outer ``@when`` body sees ``marker``
    only inside a nested ``@when`` body, and the transpiler does not
    forward outer captures into nested behaviours' capture tuples,
    so the worker raises ``NameError`` on ``marker``.
    """
    @when(c_pin)
    def _(c_pin):
        send("done", marker)


def _coverage_kickoff(c_pin, work_cowns, marker):
    """Body that produces ``len(work_cowns) - 1`` ``pushed_local`` evictions.

    The pin cown ``c_pin`` round-robins this kickoff onto a specific
    worker. Inside the body the worker dispatches one trivial
    behaviour per ``work_cowns`` entry; because every ``work_cowns``
    entry is independent (no MCS contention) each dispatch reaches
    ``boc_sched_dispatch`` immediately. The first lands in
    ``pending``; every subsequent dispatch evicts the prior
    ``pending`` into the worker's local queue, bumping
    ``pushed_local`` once per eviction. ``_coverage_done`` then
    schedules the completion signal on ``c_pin``.

    Same-cown chains do **not** exercise this path: successors on a
    held cown queue on the cown's MCS waiting list and are released
    one-at-a-time, so ``pending`` is always empty when each is
    resolved.
    """
    @when(c_pin)
    def _(c_pin):
        for wc in work_cowns:
            @when(wc)
            def _(wc):
                wc.value.count += 1
        _coverage_done(c_pin, marker)


def _busy_step(c, remaining):
    """Self-perpetuating chain that does ~50 µs of CPU work per step.

    Each step performs a short busy loop (deliberately CPU-bound, no
    sleeps) and then either schedules its own successor on the same
    cown (producer-local, so the chain stays pinned to one worker)
    or sends ``"done"`` once ``remaining`` reaches zero.
    """
    @when(c)
    def _(c):
        s = 0
        for i in range(20_000):
            s += i
        c.value.count = s
        if remaining > 1:
            _busy_step(c, remaining - 1)
        else:
            send("done", remaining)


# ---------------------------------------------------------------------------
# TLS coverage: every worker exercises the pending-eviction path
# ---------------------------------------------------------------------------


class TestTLSCoverage:
    """Every registered worker must reach producer-local pushed_local>0."""

    @classmethod
    def teardown_class(cls):
        wait()
        drain("done")

    def test_every_worker_reaches_pushed_local(self):
        """W pin cowns dispatched round-robin pin one kickoff per worker.

        With W workers and W independent pin cowns the round-robin
        cursor in ``boc_sched_dispatch``'s off-worker arm assigns one
        kickoff to each worker. Each kickoff body runs on its worker
        and dispatches K trivial behaviours against K independent
        ``work_cowns`` — those reach ``boc_sched_dispatch`` directly
        (no MCS contention), so the second through Kth dispatches
        each evict ``pending`` and bump ``pushed_local``. Stats are
        snapshotted **before** ``wait()`` because the per-worker
        array is freed during teardown.
        """
        _ensure_quiesced()
        W = 4  # noqa: N806
        K = 8  # noqa: N806
        bocpy.start(worker_count=W)
        try:
            pin_cowns = [Cown(_Counter()) for _ in range(W)]
            work_cowns_per_pin = [
                [Cown(_Counter()) for _ in range(K)] for _ in range(W)
            ]
            for i, (cp, wcs) in enumerate(zip(pin_cowns, work_cowns_per_pin)):
                _coverage_kickoff(cp, wcs, i)

            # Wait for every kickoff to finish before snapshotting stats.
            for _ in range(W):
                tag, _payload = receive("done", RECEIVE_TIMEOUT)
                assert tag != TIMEOUT, "a kickoff failed to complete"

            stats = _core.scheduler_stats()
            assert len(stats) == W, stats
            for s in stats:
                # TLS coverage: every worker must have done *some* work
                # that touches its TLS slots. Two equivalent paths
                # qualify:
                #   * `pushed_local > 0` — the worker reached the
                #     producer-local arm of `boc_sched_dispatch`
                #     (touches `current_worker`, `pending`).
                #   * `popped_via_steal > 0` — the worker reached the
                #     work-stealing path in `pop_slow` (touches
                #     `current_worker`, `steal_victim`, and the
                #     subsequent splice onto `self->q`).
                # `popped_local > 0` alone is insufficient: a stolen
                # node is returned directly from `pop_slow` and does
                # not bump `popped_local`.
                assert s["pushed_local"] > 0 or s["popped_via_steal"] > 0, (
                    f"worker {s['worker_index']} did no TLS-touching "
                    f"work (no local push, no steal): {s}"
                )
        finally:
            drain("done")
            wait()


# ---------------------------------------------------------------------------
# Runtime re-entry
# ---------------------------------------------------------------------------


class TestRuntimeReentry:
    """``start()`` / ``wait()`` / ``start()`` runs two clean workloads."""

    @classmethod
    def teardown_class(cls):
        wait()
        drain("done")

    def test_start_wait_start_runs_two_workloads(self):
        """Two independent workloads bracketed by start/wait/start/wait.

        The worker pool, terminator, and per-worker queues all spin
        up cleanly on a second ``start()`` after a prior ``wait()``
        torn the runtime down. A workload that hangs or drops
        messages on the second run indicates state leaked across the
        cycle.
        """
        _ensure_quiesced()

        # First workload.
        bocpy.start(worker_count=2)
        try:
            c = Cown(_Counter())
            for _ in range(50):
                @when(c)
                def _(c):
                    c.value.count += 1
                    send("done", c.value.count)
            for _ in range(50):
                tag, _payload = receive("done", RECEIVE_TIMEOUT)
                assert tag != TIMEOUT, "first workload stalled"
        finally:
            drain("done")
            wait()

        assert _core.scheduler_stats() == []

        # Second workload after teardown — must come up clean.
        bocpy.start(worker_count=2)
        try:
            c = Cown(_Counter())
            for _ in range(50):
                @when(c)
                def _(c):
                    c.value.count += 1
                    send("done", c.value.count)
            for _ in range(50):
                tag, _payload = receive("done", RECEIVE_TIMEOUT)
                assert tag != TIMEOUT, "second workload stalled"
        finally:
            drain("done")
            wait()


# ---------------------------------------------------------------------------
# Paired-release on uncaught body exception
# ---------------------------------------------------------------------------


def _raising_step(c):
    """Body that raises ``RuntimeError`` after touching the cown."""
    @when(c)
    def _(c):
        c.value.count += 1
        raise RuntimeError("intentional failure")


def _follow_on(c):
    """Follow-on behaviour that must observe the cown re-acquirable."""
    @when(c)
    def _(c):
        c.value.count += 1
        send("done", c.value.count)


class TestPairedRelease:
    """An uncaught body exception must still release the cown."""

    @classmethod
    def teardown_class(cls):
        wait()
        drain("done")

    def test_cown_reacquirable_after_uncaught_exception(self):
        """A failing behaviour releases its cown so the next one runs.

        ``run_behavior`` in ``worker.py`` catches ``Exception`` and
        funnels it to ``Cown.set_exception``, then runs the
        release/release_all pair. If the release path were broken the
        follow-on ``@when(c)`` would block forever; the test would
        time out on ``receive`` instead of returning a count of 2.
        """
        _ensure_quiesced()
        bocpy.start(worker_count=2)
        try:
            c = Cown(_Counter())
            _raising_step(c)
            _follow_on(c)

            tag, payload = receive("done", RECEIVE_TIMEOUT)
            assert tag != TIMEOUT, (
                "cown was not re-acquired after an uncaught exception"
            )
            assert payload == 2, payload
        finally:
            drain("done")
            wait()


# ---------------------------------------------------------------------------
# Cross-worker wake: parked W1 wakes when a busy W0 lets us schedule
# ---------------------------------------------------------------------------


def _sleep_step(c, duration):
    """Body that sleeps ``duration`` seconds and then sends a timestamp."""
    @when(c)
    def _(c):
        time.sleep(duration)
        send("done", ("sleep", time.monotonic()))


def _quick_step(c):
    """Body that immediately reports its completion timestamp."""
    @when(c)
    def _(c):
        send("done", ("quick", time.monotonic()))


class TestCrossWorkerWake:
    """A parked worker must be woken when new work is published."""

    @classmethod
    def teardown_class(cls):
        wait()
        drain("done")

    def test_parked_worker_wakes_for_other_cown(self):
        """W=2: long sleep on cown_a, then quick on cown_b.

        Round-robin from main puts cown_a's behaviour on W0 and
        cown_b's on W1. While W0 sleeps, W1 has nothing to do and
        parks. When main publishes cown_b's behaviour, W1's condvar
        must be signalled so the quick behaviour completes well
        before the sleeper does.
        """
        _ensure_quiesced()
        bocpy.start(worker_count=2)
        try:
            sleep_dur = 0.30
            cown_a = Cown(_Counter())
            cown_b = Cown(_Counter())

            t0 = time.monotonic()
            _sleep_step(cown_a, sleep_dur)
            # Give W1 a moment to actually reach cnd_wait.
            time.sleep(0.05)
            _quick_step(cown_b)

            results = {}
            for _ in range(2):
                tag, payload = receive("done", RECEIVE_TIMEOUT)
                assert tag != TIMEOUT, "behaviour failed to complete"
                kind, ts = payload
                results[kind] = ts - t0

            # The quick behaviour must finish well before the sleeper.
            # If the parked worker were not woken it would only
            # observe the work after the sleeper finishes (~sleep_dur).
            assert results["quick"] < sleep_dur * 0.7, (
                f"quick={results['quick']:.3f}s, "
                f"sleep={results['sleep']:.3f}s — parked worker did "
                f"not wake on cross-worker dispatch"
            )
        finally:
            drain("done")
            wait()


# ---------------------------------------------------------------------------
# Parked-peer CPU criterion
# ---------------------------------------------------------------------------


class TestParkedPeerCpu:
    """W=2, work pinned to W0: W1 must not spin (CPU/wall ratio < 1.5)."""

    @classmethod
    def teardown_class(cls):
        wait()
        drain("done")

    @pytest.mark.skipif(
        not hasattr(time, "process_time"),
        reason="needs time.process_time for CPU accounting",
    )
    def test_idle_worker_does_not_spin(self):
        """Pin a busy chain to a single cown and measure process CPU.

        A self-scheduling chain on one cown stays on whichever worker
        gets the kickoff (producer-locality keeps successors local).
        With W=2 the second worker has no work for the duration of
        the chain and must park in ``cnd_wait``. If the parker spun
        instead the process CPU time over the wall window would
        approach ``2 × wall``; we conservatively assert ``< 1.5×``
        to leave headroom for main, the noticeboard thread, and
        sub-interpreter overhead.
        """
        _ensure_quiesced()
        bocpy.start(worker_count=2)
        try:
            # Tune step count so the run takes at least ~0.5s of wall
            # time on a typical CI box; the assertion is a ratio so
            # absolute duration only matters for measurement noise.
            c = Cown(_Counter())
            wall_start = time.monotonic()
            cpu_start = time.process_time()
            _busy_step(c, 800)

            tag, _payload = receive("done", RECEIVE_TIMEOUT)
            assert tag != TIMEOUT

            wall_elapsed = time.monotonic() - wall_start
            cpu_elapsed = time.process_time() - cpu_start

            assert wall_elapsed > 0.2, (
                f"workload ran too briefly to measure ({wall_elapsed:.3f}s); "
                f"bump _busy_step iteration count"
            )
            ratio = cpu_elapsed / wall_elapsed
            assert ratio < 1.5, (
                f"CPU/wall ratio = {ratio:.2f} (cpu={cpu_elapsed:.3f}s, "
                f"wall={wall_elapsed:.3f}s) — idle worker is likely "
                f"spinning instead of parking in cnd_wait"
            )
        finally:
            drain("done")
            wait()
