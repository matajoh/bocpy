"""Residual public-surface tests for the per-worker scheduler.

This file hosts the contract tests for the production
``_core.scheduler_*`` endpoints that have no equivalent in the
integration suite. The bulk of scheduler behaviour is covered
end-to-end by:

  - ``test/test_scheduler_integration.py`` (park/unpark protocol,
    TLS coverage, re-entry, paired release, cross-worker wake).
  - ``test/test_scheduler_steal.py`` (steal fairness, empty-queue
    spin, fairness-arm placement).
  - ``test/test_scheduling_stress.py`` (chained dispatch, batch-size
    fairness, producer-locality under load).

The single test that remains here exercises the over-registration
contract on ``scheduler_worker_register``, which is hard to reach
without calling that endpoint directly from a non-worker thread.
"""

import pytest

import bocpy
from bocpy import _core


def test_over_registration_raises_runtime_error():
    """An extra register() beyond worker_count must raise RuntimeError.

    With self-allocating registration, the failure mode is
    over-registration. Production callers (``worker.py``) trust that
    this raises rather than silently corrupting state.
    """
    bocpy.start()
    try:
        # Workers have already registered; one more must fail.
        with pytest.raises(RuntimeError, match="over-registration"):
            _core.scheduler_worker_register()
    finally:
        bocpy.wait()
