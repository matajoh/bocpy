"""Smoke tests for `_core.scheduler_stats()` and `_core.queue_stats()`.

These tests verify:
- shape of the two snapshots (no crash on empty),
- that ``scheduler_stats()`` is empty when the runtime is down,
- that ``queue_stats()`` reflects ``set_tags`` and increments under
  ``send`` / ``receive``,
- monotonicity across two consecutive snapshots,
- that calling either accessor has no observable side effects on the
  next snapshot's counters.
"""

from bocpy import _core, drain, receive, send, set_tags


SCHEDULER_FIELDS = {
    "worker_index",
    "pushed_local",
    "pushed_remote",
    "popped_local",
    "popped_via_steal",
    "enqueue_cas_retries",
    "dequeue_cas_retries",
}

QUEUE_FIELDS = {
    "queue_index",
    "tag",
    "enqueue_cas_retries",
    "dequeue_cas_retries",
    "pushed_total",
    "popped_total",
}


def test_scheduler_stats_empty_when_runtime_down():
    """With the runtime down, the snapshot must be an empty list."""
    stats = _core.scheduler_stats()
    assert isinstance(stats, list)
    assert stats == []


def test_queue_stats_reflects_set_tags_and_traffic():
    """`queue_stats` should expose tagged queues with monotonic counters."""
    set_tags(["t_one", "t_two"])
    # Drain in case a previous test sent on these tags.
    drain(["t_one", "t_two"])

    before = _core.queue_stats()
    by_tag_before = {q["tag"]: q for q in before}
    assert "t_one" in by_tag_before
    assert "t_two" in by_tag_before
    for q in before:
        assert QUEUE_FIELDS == set(q.keys())
        assert isinstance(q["queue_index"], int)
        assert isinstance(q["pushed_total"], int)
        assert isinstance(q["popped_total"], int)
        assert q["pushed_total"] >= 0
        assert q["popped_total"] >= 0

    pushed_before = by_tag_before["t_one"]["pushed_total"]
    popped_before = by_tag_before["t_one"]["popped_total"]

    send("t_one", "alpha")
    send("t_one", "beta")
    msg = receive("t_one")
    assert msg == ("t_one", "alpha")

    after = _core.queue_stats()
    by_tag_after = {q["tag"]: q for q in after}
    assert by_tag_after["t_one"]["pushed_total"] == pushed_before + 2
    assert by_tag_after["t_one"]["popped_total"] == popped_before + 1
    # Other tag must not move.
    assert (by_tag_after["t_two"]["pushed_total"]
            == by_tag_before["t_two"]["pushed_total"])
    assert (by_tag_after["t_two"]["popped_total"]
            == by_tag_before["t_two"]["popped_total"])


def test_queue_stats_monotonic_and_no_side_effect():
    """Calling the snapshots must not perturb the counters."""
    set_tags(["t_idle"])
    drain(["t_idle"])

    snap1 = _core.queue_stats()
    snap2 = _core.queue_stats()
    snap3 = _core.queue_stats()

    by_tag = lambda snap: {q["tag"]: q for q in snap}  # noqa: E731
    s1 = by_tag(snap1)
    s2 = by_tag(snap2)
    s3 = by_tag(snap3)

    # No traffic between snapshots → counters are stable.
    for tag in s1:
        assert s2[tag]["pushed_total"] == s1[tag]["pushed_total"]
        assert s2[tag]["popped_total"] == s1[tag]["popped_total"]
        assert s3[tag]["pushed_total"] == s1[tag]["pushed_total"]
        assert s3[tag]["popped_total"] == s1[tag]["popped_total"]

    # And calling scheduler_stats does not perturb queue_stats either.
    _ = _core.scheduler_stats()
    snap4 = _core.queue_stats()
    s4 = by_tag(snap4)
    for tag in s1:
        assert s4[tag]["pushed_total"] == s1[tag]["pushed_total"]
        assert s4[tag]["popped_total"] == s1[tag]["popped_total"]


def test_drain_does_not_decrement_pushed_or_popped_total():
    """`drain` must clear messages without decrementing the counters.

    The counters track *cumulative* traffic for the lifetime of the
    process; drain is an administrative operation, not a dequeue.
    """
    set_tags(["t_drain"])
    drain(["t_drain"])

    send("t_drain", "x")
    send("t_drain", "y")

    before = next(q for q in _core.queue_stats() if q["tag"] == "t_drain")
    drain(["t_drain"])
    after = next(q for q in _core.queue_stats() if q["tag"] == "t_drain")

    # Drain pulls the messages out via boc_dequeue, so popped_total
    # advances. pushed_total must not retreat.
    assert after["pushed_total"] == before["pushed_total"]
    assert after["popped_total"] >= before["popped_total"]
