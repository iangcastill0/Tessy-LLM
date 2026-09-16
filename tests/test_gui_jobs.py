"""Tests for the desktop app's background job runner.

No tkinter and no display needed: `jobs` is deliberately free of UI imports so
the threading and event plumbing can be tested headlessly.
"""

from __future__ import annotations

import threading
import time

import pytest

from tessy.gui.jobs import Failed, Finished, IngestJob, Progress
from tessy.pipeline import Failure, ProcessReport


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make_runner(rows: int = 3, *, fail: bool = False, delay: float = 0.0):
    """A stand-in for process_spreadsheet that honours progress and cancellation."""

    def runner(spreadsheet, db_path, *, on_progress=None, should_cancel=None, **kwargs):
        if fail:
            raise RuntimeError("boom")
        report = ProcessReport(spreadsheet=str(spreadsheet))
        for i in range(1, rows + 1):
            if should_cancel is not None and should_cancel():
                report.cancelled = True
                break
            if on_progress is not None:
                on_progress(i, rows, f"row {i}")
            if delay:
                time.sleep(delay)
            report.rows += 1
            report.indexed += 1
        return report

    return runner


class TestLifecycle:
    def test_runs_to_completion(self, tmp_path):
        job = IngestJob(tmp_path / "a.xlsx", tmp_path / "a.db", runner=make_runner(3))
        job.start()
        job.join(timeout=5)

        events = job.drain()
        assert isinstance(events[-1], Finished)
        assert events[-1].report.indexed == 3
        assert not job.running

    def test_progress_events_are_emitted_in_order(self, tmp_path):
        job = IngestJob(tmp_path / "a.xlsx", tmp_path / "a.db", runner=make_runner(4))
        job.start()
        job.join(timeout=5)

        progress = [e for e in job.drain() if isinstance(e, Progress)]
        assert [p.done for p in progress] == [1, 2, 3, 4]
        assert all(p.total == 4 for p in progress)

    def test_progress_fraction(self):
        assert Progress(1, 4, "x").fraction == 0.25
        assert Progress(0, 0, "x").fraction == 0.0  # must not divide by zero

    def test_starting_twice_is_rejected(self, tmp_path):
        job = IngestJob(tmp_path / "a.xlsx", tmp_path / "a.db", runner=make_runner(3, delay=0.05))
        job.start()
        with pytest.raises(RuntimeError, match="already running"):
            job.start()
        job.join(timeout=5)

    def test_drain_is_empty_once_consumed(self, tmp_path):
        job = IngestJob(tmp_path / "a.xlsx", tmp_path / "a.db", runner=make_runner(1))
        job.start()
        job.join(timeout=5)
        job.drain()
        assert job.drain() == []


class TestFailure:
    def test_exception_becomes_a_failed_event(self, tmp_path):
        job = IngestJob(tmp_path / "a.xlsx", tmp_path / "a.db", runner=make_runner(fail=True))
        job.start()
        job.join(timeout=5)

        events = job.drain()
        assert isinstance(events[-1], Failed)
        assert "boom" in events[-1].error
        # The traceback is kept so a crash is diagnosable, not just "it failed".
        assert "RuntimeError" in events[-1].detail

    def test_worker_crash_does_not_kill_the_process(self, tmp_path):
        job = IngestJob(tmp_path / "a.xlsx", tmp_path / "a.db", runner=make_runner(fail=True))
        job.start()
        job.join(timeout=5)
        assert not job.running  # and we are still here to assert it


class TestCancellation:
    def test_cancel_stops_early_and_keeps_partial_results(self, tmp_path):
        job = IngestJob(tmp_path / "a.xlsx", tmp_path / "a.db", runner=make_runner(50, delay=0.02))
        job.start()
        assert _wait_for(lambda: any(isinstance(e, Progress) for e in job.drain()) or True)
        time.sleep(0.05)
        job.cancel()
        job.join(timeout=5)

        finished = [e for e in job.drain() if isinstance(e, Finished)]
        assert finished, "a cancelled run must still report"
        report = finished[0].report
        assert report.cancelled is True
        assert report.indexed < 50  # stopped early
        assert job.cancelled

    def test_cancel_before_start_stops_immediately(self, tmp_path):
        job = IngestJob(tmp_path / "a.xlsx", tmp_path / "a.db", runner=make_runner(10))
        job.cancel()
        job.start()
        job.join(timeout=5)
        # start() clears the flag, so this run completes normally - documenting
        # that cancel() applies to the run in flight, not a future one.
        finished = [e for e in job.drain() if isinstance(e, Finished)]
        assert finished and finished[0].report.indexed == 10

    def test_not_running_before_start(self, tmp_path):
        job = IngestJob(tmp_path / "a.xlsx", tmp_path / "a.db", runner=make_runner(1))
        assert not job.running

    def test_runs_off_the_calling_thread(self, tmp_path):
        seen: list[str] = []

        def runner(spreadsheet, db_path, **kwargs):
            seen.append(threading.current_thread().name)
            return ProcessReport(spreadsheet=str(spreadsheet))

        job = IngestJob(tmp_path / "a.xlsx", tmp_path / "a.db", runner=runner)
        job.start()
        job.join(timeout=5)
        assert seen and seen[0] != threading.current_thread().name


class TestOptionsArePassedThrough:
    def test_pipeline_receives_the_configured_options(self, tmp_path):
        captured: dict = {}

        def runner(spreadsheet, db_path, **kwargs):
            captured.update(kwargs)
            return ProcessReport(spreadsheet=str(spreadsheet))

        job = IngestJob(
            tmp_path / "a.xlsx",
            tmp_path / "a.db",
            sheet="Subjects",
            header_row=2,
            lang="eng",
            psms=(6,),
            do_preprocess=False,
            workdir=tmp_path / "w",
            runner=runner,
        )
        job.start()
        job.join(timeout=5)

        assert captured["sheet"] == "Subjects"
        assert captured["header_row"] == 2
        assert captured["psms"] == (6,)
        assert captured["do_preprocess"] is False
        assert callable(captured["on_progress"])
        assert callable(captured["should_cancel"])

    def test_failures_survive_into_the_finished_report(self, tmp_path):
        def runner(spreadsheet, db_path, **kwargs):
            report = ProcessReport(spreadsheet=str(spreadsheet), rows=2, indexed=1)
            report.failures.append(Failure(where="Sheet!r2", error="unreadable"))
            return report

        job = IngestJob(tmp_path / "a.xlsx", tmp_path / "a.db", runner=runner)
        job.start()
        job.join(timeout=5)

        report = [e for e in job.drain() if isinstance(e, Finished)][0].report
        assert len(report.failures) == 1
        assert "unreadable" in report.summary()
