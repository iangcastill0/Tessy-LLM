"""Smoke tests for the tkinter window.

These build the real widgets against a real display, so they catch breakage the
headless job tests cannot - notably the image preview, where tkinter keeps only
a weak reference to a PhotoImage and a dropped one renders blank.

Skipped automatically when there is no display; CI runs them under xvfb-run.
"""

from __future__ import annotations

import pytest

from tessy.index import TessyIndex

pytestmark = pytest.mark.requires_display


@pytest.fixture
def indexed_db(tmp_path, default_spec, texas_spec):
    """An index holding one clean record and one flagged record with an image."""
    from synthetic import render_licence

    image = render_licence(tmp_path / "shown.png", default_spec)
    db = tmp_path / "case.db"
    with TessyIndex(db) as index:
        index.add_document(
            source=str(tmp_path / "case.xlsx"),
            sheet="Subjects",
            row=2,
            image_path=str(image),
            image_origin="embedded",
            ocr_text="CALIFORNIA DRIVER LICENSE DL 11234562 CARDHOLDER",
            ocr_confidence=94.2,
            ocr_psm=11,
            sheet_text="Case Ref: CASE-1002",
            fields={
                "licence_no": "11234562",
                "full_name": "ALEXANDER J CARDHOLDER",
                "last_name": "CARDHOLDER",
                "dob": "1977-08-31",
                "jurisdiction": "CALIFORNIA",
                "completeness": 1.0,
                "warnings": ["licence_no is all digits"],
            },
        )
        index.add_document(
            source=str(tmp_path / "case.xlsx"),
            sheet="Subjects",
            row=3,
            image_path=None,
            ocr_text="TEXAS RIVERA",
            ocr_confidence=91.0,
            fields={
                "full_name": "MARIA L RIVERA",
                "licence_no": "T4459981",
                "jurisdiction": "TEXAS",
                "warnings": [],
            },
        )
    return db


@pytest.fixture
def app(indexed_db):
    import tkinter as tk

    from tessy.gui.app import TessyApp

    root = tk.Tk()
    root.withdraw()  # keep it off-screen during tests
    instance = TessyApp(root, db_path=indexed_db)
    yield instance
    root.destroy()


class TestWindow:
    def test_builds_and_lists_records(self, app):
        assert len(app.tree.get_children()) == 2

    def test_flagged_rows_are_tagged(self, app):
        tagged = [
            item for item in app.tree.get_children() if "flagged" in app.tree.item(item, "tags")
        ]
        assert len(tagged) == 1  # only the record carrying a warning

    def test_status_bar_reports_the_count(self, app):
        assert "2" in app.status.cget("text")


class TestSelection:
    def test_selecting_a_record_fills_the_detail_pane(self, app):
        first = app.tree.get_children()[0]
        app.tree.selection_set(first)
        app.on_select()
        text = app.detail.get("1.0", "end")
        assert "ALEXANDER J CARDHOLDER" in text
        assert "1977-08-31" in text

    def test_warnings_are_shown(self, app):
        app.tree.selection_set(app.tree.get_children()[0])
        app.on_select()
        assert "Needs checking" in app.detail.get("1.0", "end")

    def test_image_preview_is_retained(self, app):
        """The PhotoImage must be held on the instance or tkinter blanks it."""
        app.tree.selection_set(app.tree.get_children()[0])
        app.on_select()
        assert app._preview_image is not None

    def test_row_without_an_image_shows_a_placeholder(self, app):
        target = next(
            item
            for item in app.tree.get_children()
            if "RIVERA" in str(app.tree.item(item, "values"))
        )
        app.tree.selection_set(target)
        app.on_select()
        assert app._preview_image is None
        assert "no image" in app.preview.cget("text")

    def test_missing_image_file_does_not_raise(self, app):
        app._show_preview("/nonexistent/path/to/scan.png")
        assert app._preview_image is None


class TestFiltering:
    def test_search_narrows_the_list(self, app):
        app.query.set("RIVERA")
        app.refresh()
        assert len(app.tree.get_children()) == 1

    def test_clear_restores_everything(self, app):
        app.query.set("RIVERA")
        app.refresh()
        app.clear_search()
        assert len(app.tree.get_children()) == 2

    def test_review_view_shows_only_flagged(self, app):
        app.view.set("review")
        app.refresh()
        assert len(app.tree.get_children()) == 1

    def test_no_match_empties_the_list_without_error(self, app):
        app.query.set("NOBODYHERE")
        app.refresh()
        assert app.tree.get_children() == ()

    def test_missing_index_is_handled(self, app, tmp_path):
        app.db_path = tmp_path / "does_not_exist.db"
        app.refresh()
        assert app.tree.get_children() == ()
        assert "No index" in app.status.cget("text")


class TestRunOutcomes:
    """The handlers that fire when a background run ends."""

    def _report(self, **kw):
        from tessy.pipeline import ProcessReport

        report = ProcessReport(spreadsheet="case.xlsx", rows=4, indexed=4, **kw)
        report.confidences.extend([94.0, 90.0])
        return report

    def test_finished_updates_status_and_reenables_run(self, app, monkeypatch):
        monkeypatch.setattr(app, "tesseract_ok", True)
        app.run_button.config(state="disabled")
        app._on_finished(self._report())

        assert "Indexed 4 document(s)" in app.status.cget("text")
        assert str(app.run_button.cget("state")) == "normal"
        assert str(app.stop_button.cget("state")) == "disabled"
        assert app.job is None

    def test_cancelled_run_says_so(self, app, monkeypatch):
        monkeypatch.setattr(app, "tesseract_ok", True)
        app._on_finished(self._report(cancelled=True))
        assert "Cancelled after" in app.status.cget("text")

    def test_failures_are_reported_without_blocking(self, app, monkeypatch):
        from tessy.pipeline import Failure

        shown: list[tuple] = []
        monkeypatch.setattr("tkinter.messagebox.showwarning", lambda *a, **k: shown.append(a))
        monkeypatch.setattr(app, "tesseract_ok", True)

        report = self._report()
        report.failures.append(Failure(where="Subjects!r3", error="unreadable"))
        app._on_finished(report)

        assert shown, "the operator must be told which images failed"
        assert "failure(s)" in app.status.cget("text")

    def test_failed_run_surfaces_the_error(self, app, monkeypatch):
        from tessy.gui.jobs import Failed

        errors: list[tuple] = []
        monkeypatch.setattr("tkinter.messagebox.showerror", lambda *a, **k: errors.append(a))
        monkeypatch.setattr(app, "tesseract_ok", True)

        app._on_failed(Failed("spreadsheet is corrupt", "traceback..."))

        assert errors
        assert "spreadsheet is corrupt" in app.status.cget("text")
        assert app.job is None

    def test_run_without_a_spreadsheet_warns(self, app, monkeypatch):
        warned: list[tuple] = []
        monkeypatch.setattr("tkinter.messagebox.showwarning", lambda *a, **k: warned.append(a))
        app.spreadsheet = None
        app.run_ingest()
        assert warned
        assert app.job is None

    def test_poll_with_no_job_is_a_noop(self, app):
        app.job = None
        app._poll_job()  # must not raise


class TestExport:
    def test_exports_every_indexed_record(self, app, tmp_path, monkeypatch):
        target = tmp_path / "out.csv"
        monkeypatch.setattr("tkinter.filedialog.asksaveasfilename", lambda **k: str(target))
        app.export_csv()

        lines = target.read_text().strip().splitlines()
        assert lines[0].startswith("id,source,sheet,row")
        assert len(lines) == 3  # header + 2 records

    def test_export_without_an_index_warns(self, app, tmp_path, monkeypatch):
        warned: list[tuple] = []
        monkeypatch.setattr("tkinter.messagebox.showwarning", lambda *a, **k: warned.append(a))
        app.db_path = tmp_path / "missing.db"
        app.export_csv()
        assert warned

    def test_cancelled_dialog_writes_nothing(self, app, monkeypatch, tmp_path):
        monkeypatch.setattr("tkinter.filedialog.asksaveasfilename", lambda **k: "")
        app.export_csv()
        assert not list(tmp_path.glob("*.csv"))


class TestProgressBar:
    def test_uses_a_high_contrast_style(self, app):
        """The stock clam fill is near-invisible against the frame.

        Without an explicit colour a finished run looks like an empty bar, so
        the custom style is load-bearing rather than decoration.
        """
        from tkinter import ttk

        assert str(app.progress.cget("style")) == "Tessy.Horizontal.TProgressbar"
        configured = ttk.Style().configure("Tessy.Horizontal.TProgressbar")
        assert configured["background"] != configured["troughcolor"]

    def test_progress_events_move_the_bar(self, app):
        from tessy.gui.jobs import Progress

        app.job = type(
            "J", (), {"drain": lambda self: [Progress(2, 4, "row 2")], "running": True}
        )()
        app._poll_job()
        assert float(app.progress.cget("value")) == 2.0
        assert float(app.progress.cget("maximum")) == 4.0
        assert "[2/4]" in app.status.cget("text")
        app.job = None
