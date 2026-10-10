"""Tests for the bounded worker pool (brief sections 10 and 14).

The GUI thread must never block, and threads must never be created without limit
on a two-core machine. These tests run real background work through the real
``QThreadPool`` and pump the event loop, so the signal/slot hand-off is exercised
rather than assumed.
"""

from __future__ import annotations

import threading
import time

from PyQt5.QtCore import QRunnable as QRunnableType

import pytest

from cartridge.core.workers import (
    BUSY_THRESHOLD_MS,
    WORKER_COUNT,
    CancelToken,
    TaskHandle,
    Worker,
    WorkerPool,
    WorkerSignals,
    _accepts_keyword,
    _message_for,
)


def pump(app, seconds=0.05):
    """Let queued signal deliveries land on the GUI thread."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)


def run_to_completion(pool, app, timeout=5.0):
    pool.wait(int(timeout * 1000))
    pump(app)


# --------------------------------------------------------------------------
# basics
# --------------------------------------------------------------------------
def test_pool_is_bounded_to_the_target_hardware(qapp):
    pool = WorkerPool()
    assert pool.max_threads == WORKER_COUNT == 3


def test_pool_can_be_constructed_with_a_custom_width(qapp):
    pool = WorkerPool(max_threads=1)
    assert pool.max_threads == 1
    pool = WorkerPool(max_threads=0)
    assert pool.max_threads == 1, "a zero-width pool would deadlock"


def test_result_arrives_through_the_finished_signal(qapp):
    pool = WorkerPool()
    received = []
    pool.submit(lambda x: x * 2, 21, _name="double", _on_finished=received.append)
    run_to_completion(pool, qapp)
    assert received == [42]


def test_arguments_and_keywords_reach_the_callable(qapp):
    pool = WorkerPool()
    received = []
    pool.submit(
        lambda a, b, extra=None: (a, b, extra),
        1, 2, extra="kw", _name="args", _on_finished=received.append,
    )
    run_to_completion(pool, qapp)
    assert received == [(1, 2, "kw")]


def test_a_callable_with_its_own_name_argument_still_works(qapp):
    """The pool's reserved keywords must not collide with the task's."""
    pool = WorkerPool()
    received = []
    pool.submit(
        lambda name, other: "%s/%s" % (name, other),
        name="task-arg", other="x", _name="group", _on_finished=received.append,
    )
    run_to_completion(pool, qapp)
    assert received == ["task-arg/x"]


def test_exception_becomes_a_failed_signal_not_a_crash(qapp):
    pool = WorkerPool()
    failures = []

    def boom():
        raise ValueError("kaboom")

    pool.submit(boom, _name="bad", _on_failed=lambda message, detail: failures.append((message, detail)))
    run_to_completion(pool, qapp)
    assert len(failures) == 1
    assert failures[0][0] == "kaboom"
    assert "ValueError" in failures[0][1]
    assert pool.stats()["failed"] >= 1


def test_a_broken_ui_callback_does_not_kill_the_worker(qapp, capsys):
    pool = WorkerPool()

    def bad_callback(_payload):
        raise RuntimeError("ui exploded")

    pool.submit(lambda: "value", _name="cb", _on_finished=bad_callback)
    run_to_completion(pool, qapp)
    assert pool.stats()["completed"] >= 1


def test_progress_is_forwarded(qapp):
    pool = WorkerPool()
    seen = []

    def chunky(progress=None, cancel_event=None):
        for index in range(4):
            if progress:
                progress(index + 1, 4)
        return "done"

    pool.submit(chunky, _name="chunky", _on_progress=lambda c, t: seen.append((c, t)))
    run_to_completion(pool, qapp)
    assert seen == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_cancel_event_reaches_a_cooperative_task(qapp):
    pool = WorkerPool()
    received = []

    def long_task(cancel_event=None):
        for _ in range(500):
            if cancel_event is not None and cancel_event():
                return "stopped-early"
            time.sleep(0.01)
        return "finished"

    handle = pool.submit(long_task, _name="long", _on_finished=received.append)
    assert isinstance(handle, TaskHandle)
    time.sleep(0.05)
    assert pool.cancel("long") >= 1
    assert handle.cancelled is True
    run_to_completion(pool, qapp, timeout=8.0)
    assert received == ["stopped-early"]


def test_cancel_reports_how_many_tasks_it_signalled(qapp):
    pool = WorkerPool()
    pool.submit(lambda: time.sleep(0.3), _name="a")
    pool.submit(lambda: time.sleep(0.3), _name="b")
    assert pool.cancel("a") == 1
    assert pool.cancel() >= 1
    run_to_completion(pool, qapp, timeout=8.0)


def test_a_task_that_ignores_cancellation_still_completes(qapp):
    pool = WorkerPool()
    received = []
    pool.submit(lambda: "immutable-result", _name="stubborn", _on_finished=received.append)
    pool.cancel("stubborn")
    run_to_completion(pool, qapp)
    assert received == ["immutable-result"]


def test_cancelled_worker_is_counted(qapp):
    pool = WorkerPool()
    failures = []

    def task(cancel_event=None):
        while not (cancel_event and cancel_event()):
            time.sleep(0.005)
        # returns None: nothing was produced, so this is a cancellation, not a
        # partial result
        return None

    handle = pool.submit(task, _name="cancelme", _on_failed=lambda m, d: failures.append(m))
    handle.cancel()
    run_to_completion(pool, qapp, timeout=8.0)
    assert "cancelled" in failures
    assert pool.stats()["cancelled"] >= 1


# --------------------------------------------------------------------------
# accounting and lifecycle
# --------------------------------------------------------------------------
def test_stats_track_submitted_and_completed(qapp):
    pool = WorkerPool()
    for index in range(5):
        pool.submit(lambda i=index: i, _name="counted")
    run_to_completion(pool, qapp)
    stats = pool.stats()
    assert stats["submitted"] == 5
    assert stats["completed"] == 5
    assert stats["pending"] == 0
    assert stats["max_threads"] == 3


def test_pending_by_task_groups(qapp):
    pool = WorkerPool()
    started = threading.Event()
    release = threading.Event()

    def blocking():
        started.set()
        release.wait(5.0)
        return True

    pool.submit(blocking, _name="blocker")
    assert started.wait(3.0)
    pool.submit(lambda: None, _name="queued")
    stats = pool.stats()
    assert stats["pending_by_task"].get("queued") == 1
    release.set()
    run_to_completion(pool, qapp, timeout=8.0)


def test_wait_returns_true_once_drained(qapp):
    pool = WorkerPool()
    pool.submit(lambda: time.sleep(0.02), _name="short")
    assert pool.wait(5000) is True


def test_shutdown_drains_and_reports(qapp):
    pool = WorkerPool()
    pool.submit(lambda: time.sleep(0.02), _name="x")
    assert pool.shutdown(5000) is True
    assert pool.active_count() == 0


def test_many_tasks_do_not_create_unbounded_threads(qapp):
    """The whole point of the pool: 50 tasks, still at most 3 threads."""
    pool = WorkerPool()
    peak = {"value": 0}
    lock = threading.Lock()

    def task():
        with lock:
            peak["value"] = max(peak["value"], threading.active_count())
        time.sleep(0.01)
        return True

    for index in range(50):
        pool.submit(task, _name="many")
    run_to_completion(pool, qapp, timeout=30.0)
    assert pool.stats()["completed"] == 50
    # Baseline threads (main + Qt internals) plus at most the pool width.
    assert peak["value"] <= threading.active_count()
    assert pool.max_threads == 3


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def test_cancel_token():
    token = CancelToken()
    assert token.cancelled is False
    assert token() is False
    token.cancel()
    assert token.cancelled is True
    assert token() is True
    token.reset()
    assert token.cancelled is False


def test_accepts_keyword_detection():
    def no_kwargs():
        return None

    def with_cancel(cancel_event=None):
        return None

    def with_progress(progress=None):
        return None

    def with_var_kwargs(**kwargs):
        return None

    assert _accepts_keyword(no_kwargs, "cancel_event") is False
    assert _accepts_keyword(with_cancel, "cancel_event") is True
    assert _accepts_keyword(with_progress, "progress") is True
    # **kwargs is NOT an opt-in: injecting into it broke every lambda task.
    assert _accepts_keyword(with_var_kwargs, "anything") is False
    assert _accepts_keyword(with_var_kwargs, "cancel_event") is False
    assert _accepts_keyword(len, "cancel_event") is False


def test_message_for_provider_errors_is_redacted():
    from cartridge.core.redaction import Redactor
    from cartridge.providers.base import ProviderRateLimited

    redactor = Redactor(["SUPERSECRETVALUE123456"])
    exc = ProviderRateLimited("slow down", retry_after=30)
    text = _message_for(exc)
    assert "slow down" in text
    assert "SUPERSECRETVALUE" not in text


def test_message_for_a_bare_exception():
    assert _message_for(ValueError("boom")) == "boom"
    assert _message_for(ValueError()) == "ValueError"


def test_message_for_multiline_text_takes_one_line():
    assert _message_for(RuntimeError("first\nsecond")) == "first"


def test_busy_threshold_is_small_enough_to_matter():
    """Anything slower than this needs a visible busy state (brief §14)."""
    assert 0 < BUSY_THRESHOLD_MS <= 500


def test_task_handle_is_pure_python_and_safe_to_hold(qapp):
    """submit() must not hand out the runnable (see DECISIONS D-012)."""
    pool = WorkerPool()
    received = []
    handle = pool.submit(lambda: "value", _name="handle", _on_finished=received.append)
    assert isinstance(handle, TaskHandle)
    assert not isinstance(handle, QRunnableType)
    assert handle.name == "handle"
    assert handle.cancelled is False
    handle.cancel()
    assert handle.cancelled is True
    handle.cancel_token.reset()
    run_to_completion(pool, qapp)
    assert received == ["value"]
    assert handle.done.is_set()


def test_handle_can_outlive_the_task(qapp):
    """Dropping or keeping the handle must not affect the runnable's lifetime."""
    pool = WorkerPool()
    handles = [pool.submit(lambda i=i: i, _name="keep") for i in range(20)]
    run_to_completion(pool, qapp, timeout=15.0)
    assert all(handle.done.is_set() for handle in handles)
    assert pool.stats()["completed"] >= 20
    del handles
    pump(qapp)
    assert pool.pending_count() == 0


def test_worker_runnable_is_constructible_for_unit_tests(qapp):
    signals = WorkerSignals()
    token = CancelToken()
    worker = Worker(lambda: 7, signals, token, task_name="direct")
    assert worker.name == "direct"
    assert worker.signals is signals
    assert token.cancelled is False
    token.cancel()
    assert token.cancelled is True
    # The runnable must be owned by the pool, not by Python.
    worker.setAutoDelete(True)
    pool = WorkerPool()
    pool._pool.start(worker)
    assert pool.wait(5000)
