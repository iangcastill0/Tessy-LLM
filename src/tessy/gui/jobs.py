"""Background execution for the desktop app.

OCR takes roughly a second per licence, so a run of any size would freeze the
window if it happened on the UI thread. Work runs on a worker thread and reports
back through a queue that the UI drains on a timer.

Deliberately free of tkinter imports so this can be tested headlessly.
"""

from __future__ import annotations

import queue
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..ocr import DEFAULT_LANG, DEFAULT_PSMS
from ..pipeline import ProcessReport, process_spreadsheet


@dataclass
class Progress:
    """A row is about to be processed."""

    done: int
    total: int
    label: str

    @property
    def fraction(self) -> float:
        return self.done / self.total if self.total else 0.0


@dataclass
class Finished:
    """The run completed (possibly cancelled); `report` holds partial results."""

    report: ProcessReport


@dataclass
class Failed:
    """The run raised before producing a report."""

    error: str
    detail: str = ""


Event = Progress | Finished | Failed


class IngestJob:
    """One spreadsheet ingest, running on its own thread.

    The UI calls :meth:`start`, then polls :meth:`drain` on a timer and reacts to
    whatever events came back. :meth:`cancel` asks the pipeline to stop between
    rows, so work already indexed is kept rather than thrown away.
    """

    def __init__(
        self,
        spreadsheet: str | Path,
        db_path: str | Path,
        *,
        workdir: str | Path | None = None,
        sheet: str | None = None,
        header_row: int = 1,
        lang: str = DEFAULT_LANG,
        psms: tuple[int, ...] = DEFAULT_PSMS,
        do_preprocess: bool = True,
        runner: Callable[..., ProcessReport] = process_spreadsheet,
    ):
        self.spreadsheet = Path(spreadsheet)
        self.db_path = Path(db_path)
        self.options: dict[str, Any] = {
            "workdir": workdir,
            "sheet": sheet,
            "header_row": header_row,
            "lang": lang,
            "psms": psms,
            "do_preprocess": do_preprocess,
        }
        self._runner = runner
        self._events: queue.Queue[Event] = queue.Queue()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    # -- control -----------------------------------------------------------
    def start(self) -> None:
        if self.running:
            raise RuntimeError("job is already running")
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="tessy-ingest")
        self._thread.start()

    def cancel(self) -> None:
        """Ask the run to stop at the next row boundary."""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # -- events ------------------------------------------------------------
    def drain(self) -> list[Event]:
        """Return every event queued since the last call."""
        out: list[Event] = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    # -- worker ------------------------------------------------------------
    def _run(self) -> None:
        try:
            report = self._runner(
                self.spreadsheet,
                self.db_path,
                on_progress=lambda done, total, label: self._events.put(
                    Progress(done, total, label)
                ),
                should_cancel=self._cancel.is_set,
                **self.options,
            )
            self._events.put(Finished(report))
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI, not swallowed
            # A crash in a worker thread would otherwise vanish silently and the
            # window would sit at "working" forever.
            self._events.put(Failed(str(exc) or exc.__class__.__name__, traceback.format_exc()))
