"""Tkinter desktop front end for Tessy.

Tkinter rather than a packaged web view or Electron on purpose: it ships with
Python, opens no socket and pulls in no extra runtime. For tooling that handles
identity documents, "there is no server and nothing listening" is a feature, not
a limitation.

The window is built around the one thing a terminal cannot do well: showing the
licence image next to the fields that were read off it, so an examiner can
confirm or correct a record by eye.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError as exc:  # pragma: no cover - platform dependent
    raise SystemExit(
        "Tessy's desktop app needs tkinter, which is part of Python but packaged "
        "separately on some systems.\n"
        "  Debian/Ubuntu : sudo apt install python3-tk\n"
        "  Fedora        : sudo dnf install python3-tkinter\n"
        "  macOS/Windows : reinstall Python from python.org (tkinter is included)\n"
        f"(import error: {exc})"
    ) from exc

from .. import __version__
from ..index import TessyIndex
from ..ocr import DEFAULT_PSMS, TesseractNotFound, available_languages, tesseract_version
from .jobs import Failed, Finished, IngestJob, Progress

POLL_MS = 100
PREVIEW_MAX = (460, 300)

# Field order for the detail pane: identity first, then descriptors.
DETAIL_FIELDS = (
    ("licence_no", "Licence no"),
    ("full_name", "Name"),
    ("last_name", "Last name"),
    ("first_name", "First name"),
    ("dob", "Date of birth"),
    ("expiry", "Expires"),
    ("issued", "Issued"),
    ("jurisdiction", "Jurisdiction"),
    ("document_type", "Document"),
    ("licence_class", "Class"),
    ("sex", "Sex"),
    ("height", "Height"),
    ("weight", "Weight"),
    ("eyes", "Eyes"),
    ("hair", "Hair"),
    ("address", "Address"),
)


class TessyApp(ttk.Frame):
    """Main application window."""

    def __init__(self, master: tk.Tk, db_path: str | Path = "data/output/case_index.db"):
        super().__init__(master, padding=8)
        self.master: tk.Tk = master
        self.db_path = Path(db_path)
        self.spreadsheet: Path | None = None
        self.job: IngestJob | None = None
        self._preview_image = None  # must outlive the call or tkinter blanks it
        self._rows: dict[str, dict] = {}

        master.title(f"Tessy {__version__} - licence OCR and index")
        master.geometry("1180x740")
        master.minsize(940, 600)

        self.grid(row=0, column=0, sticky="nsew")
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)

        self._build_menu()
        self._build_toolbar()
        self._build_searchbar()
        self._build_body()
        self._build_statusbar()

        self._check_tesseract()
        self.refresh()

    # -- construction ------------------------------------------------------
    def _build_menu(self) -> None:
        menu = tk.Menu(self.master)

        file_menu = tk.Menu(menu, tearoff=0)
        file_menu.add_command(label="Open spreadsheet...", command=self.choose_spreadsheet)
        file_menu.add_command(label="Choose index...", command=self.choose_index)
        file_menu.add_separator()
        file_menu.add_command(label="Export CSV...", command=self.export_csv)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.on_close)
        menu.add_cascade(label="File", menu=file_menu)

        help_menu = tk.Menu(menu, tearoff=0)
        help_menu.add_command(label="Check Tesseract", command=self.show_doctor)
        menu.add_cascade(label="Help", menu=help_menu)

        self.master.config(menu=menu)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        bar.columnconfigure(3, weight=1)

        ttk.Button(bar, text="Open spreadsheet...", command=self.choose_spreadsheet).grid(
            row=0, column=0
        )
        self.run_button = ttk.Button(bar, text="Run OCR", command=self.run_ingest, state="disabled")
        self.run_button.grid(row=0, column=1, padx=(6, 0))
        self.stop_button = ttk.Button(
            bar, text="Stop", command=self.cancel_ingest, state="disabled"
        )
        self.stop_button.grid(row=0, column=2, padx=(6, 0))

        self.sheet_label = ttk.Label(bar, text="No spreadsheet selected", foreground="#666")
        self.sheet_label.grid(row=0, column=3, sticky="w", padx=12)

        self.tess_label = ttk.Label(bar, text="Tesseract: checking...")
        self.tess_label.grid(row=0, column=4, sticky="e")

    def _build_searchbar(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        bar.columnconfigure(1, weight=1)

        ttk.Label(bar, text="Search:").grid(row=0, column=0, padx=(0, 6))
        self.query = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.query)
        entry.grid(row=0, column=1, sticky="ew")
        entry.bind("<Return>", lambda _e: self.refresh())
        ttk.Button(bar, text="Search", command=self.refresh).grid(row=0, column=2, padx=6)
        ttk.Button(bar, text="Clear", command=self.clear_search).grid(row=0, column=3)

        self.view = tk.StringVar(value="all")
        ttk.Radiobutton(
            bar, text="All", value="all", variable=self.view, command=self.refresh
        ).grid(row=0, column=4, padx=(16, 0))
        ttk.Radiobutton(
            bar, text="Needs review", value="review", variable=self.view, command=self.refresh
        ).grid(row=0, column=5, padx=(6, 0))

    def _build_body(self) -> None:
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.grid(row=2, column=0, sticky="nsew")

        # -- results list
        left = ttk.Frame(panes)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        columns = ("name", "licence", "dob", "where", "conf")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        for key, heading, width in (
            ("name", "Name", 190),
            ("licence", "Licence no", 110),
            ("dob", "DOB", 95),
            ("where", "Row", 70),
            ("conf", "Conf", 60),
        ):
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        # Flagged records are tinted so the review queue reads at a glance.
        self.tree.tag_configure("flagged", background="#fff4e5")

        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        panes.add(left, weight=3)

        # -- detail pane
        right = ttk.Frame(panes, padding=(10, 0, 0, 0))
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        self.preview = ttk.Label(
            right,
            text="Select a record to see its licence image",
            anchor="center",
            relief="solid",
            borderwidth=1,
            padding=8,
            foreground="#777",
        )
        self.preview.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        detail_wrap = ttk.Frame(right)
        detail_wrap.grid(row=1, column=0, sticky="nsew")
        detail_wrap.rowconfigure(0, weight=1)
        detail_wrap.columnconfigure(0, weight=1)

        self.detail = tk.Text(
            detail_wrap,
            wrap="word",
            height=14,
            width=44,
            state="disabled",
            relief="flat",
            background="#fbfbfb",
            padx=8,
            pady=8,
        )
        self.detail.grid(row=0, column=0, sticky="nsew")
        dscroll = ttk.Scrollbar(detail_wrap, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=dscroll.set)
        dscroll.grid(row=0, column=1, sticky="ns")

        self.detail.tag_configure("label", foreground="#555")
        self.detail.tag_configure("value", font=("TkDefaultFont", 10, "bold"))
        self.detail.tag_configure("warn", foreground="#a4500f")
        self.detail.tag_configure("missing", foreground="#999")
        self.detail.tag_configure("heading", font=("TkDefaultFont", 10, "bold"))

        panes.add(right, weight=2)

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        bar.columnconfigure(0, weight=1)

        self.status = ttk.Label(bar, text="Ready", anchor="w")
        self.status.grid(row=0, column=0, sticky="ew")

        # The stock clam progressbar fills in a grey almost identical to the
        # surrounding frame, so a finished run looks like an empty bar. Give it
        # an explicit colour so progress is actually readable.
        style = ttk.Style()
        style.configure(
            "Tessy.Horizontal.TProgressbar",
            background="#2e7d32",
            troughcolor="#d6d2ca",
            bordercolor="#b5b0a8",
            lightcolor="#2e7d32",
            darkcolor="#2e7d32",
        )
        self.progress = ttk.Progressbar(
            bar, mode="determinate", length=220, style="Tessy.Horizontal.TProgressbar"
        )
        self.progress.grid(row=0, column=1, sticky="e", padx=(8, 0))

    # -- tesseract ---------------------------------------------------------
    def _check_tesseract(self) -> None:
        try:
            version = tesseract_version()
            langs = available_languages()
        except TesseractNotFound:
            self.tess_label.config(text="Tesseract: NOT FOUND", foreground="#b00020")
            self.set_status("Tesseract not found - OCR is unavailable. See Help > Check Tesseract.")
            self.tesseract_ok = False
            return

        if not langs:
            self.tess_label.config(text=f"Tesseract {version}: no languages", foreground="#b00020")
            self.tesseract_ok = False
            return

        self.tess_label.config(
            text=f"Tesseract {version} ({', '.join(langs)})", foreground="#2e7d32"
        )
        self.tesseract_ok = True

    def show_doctor(self) -> None:
        try:
            lines = [
                f"Tesseract : {tesseract_version()}",
                f"Languages : {', '.join(available_languages()) or 'NONE'}",
            ]
        except TesseractNotFound as exc:
            messagebox.showerror(
                "Tesseract not found",
                f"{exc}\n\nBuild it with scripts/build_tesseract.sh, or set "
                "TESSERACT_BIN to an existing binary.",
            )
            return
        lines.append(f"Index     : {self.db_path}")
        messagebox.showinfo("Tesseract check", "\n".join(lines))

    # -- actions -----------------------------------------------------------
    def choose_spreadsheet(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Select case spreadsheet",
            filetypes=[("Excel workbook", "*.xlsx *.xlsm"), ("All files", "*.*")],
        )
        if not chosen:
            return
        self.spreadsheet = Path(chosen)
        self.sheet_label.config(text=str(self.spreadsheet), foreground="#000")
        self.run_button.config(state="normal" if self.tesseract_ok else "disabled")
        self.set_status(f"Selected {self.spreadsheet.name}. Press Run OCR to index it.")

    def choose_index(self) -> None:
        chosen = filedialog.asksaveasfilename(
            title="Index database",
            defaultextension=".db",
            initialfile=self.db_path.name,
            filetypes=[("SQLite index", "*.db"), ("All files", "*.*")],
            confirmoverwrite=False,
        )
        if not chosen:
            return
        self.db_path = Path(chosen)
        self.set_status(f"Index: {self.db_path}")
        self.refresh()

    def run_ingest(self) -> None:
        if self.spreadsheet is None:
            messagebox.showwarning("No spreadsheet", "Choose a spreadsheet first.")
            return
        if self.job is not None and self.job.running:
            return

        self.job = IngestJob(
            self.spreadsheet,
            self.db_path,
            workdir=self.db_path.parent / "work",
            psms=DEFAULT_PSMS,
        )
        self.job.start()
        self.run_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.progress.config(value=0, maximum=100)
        self.set_status("Starting...")
        self.after(POLL_MS, self._poll_job)

    def cancel_ingest(self) -> None:
        if self.job is not None and self.job.running:
            self.job.cancel()
            self.set_status("Stopping after the current row...")
            self.stop_button.config(state="disabled")

    def _poll_job(self) -> None:
        """Drain worker events on the UI thread and reflect them in the window."""
        job = self.job
        if job is None:
            return

        # Sample `running` BEFORE draining. The other order loses a race: the
        # thread can finish between the drain and the check, and its Finished
        # event would then never be processed, leaving the UI stuck on "working".
        was_running = job.running
        for event in job.drain():
            if isinstance(event, Progress):
                self.progress.config(value=event.done, maximum=max(event.total, 1))
                self.set_status(f"[{event.done}/{event.total}] {event.label}")
            elif isinstance(event, Finished):
                self._on_finished(event.report)
                return
            elif isinstance(event, Failed):
                self._on_failed(event)
                return

        if was_running:
            self.after(POLL_MS, self._poll_job)
        else:
            self._reset_controls()

    def _on_finished(self, report) -> None:
        self._reset_controls()
        self.progress.config(value=report.indexed, maximum=max(report.rows, 1))
        verb = "Cancelled after" if report.cancelled else "Indexed"
        summary = (
            f"{verb} {report.indexed} document(s) from {report.rows} row(s) "
            f"in {report.duration_s:.1f}s - mean confidence {report.mean_confidence:.1f}"
        )
        if report.failures:
            summary += f" - {len(report.failures)} failure(s)"

        # Refresh first, then set the status: refresh() writes the record count
        # to the same label, so setting the summary before it would wipe the
        # run result the operator actually needs to see.
        self.refresh()
        self.set_status(summary)

        if report.failures:
            detail = "\n".join(f"- {f.where}: {f.error}" for f in report.failures[:12])
            messagebox.showwarning(
                "Some images could not be read",
                f"{len(report.failures)} image(s) failed. The rest were indexed.\n\n{detail}",
            )

    def _on_failed(self, event: Failed) -> None:
        self._reset_controls()
        self.set_status(f"Failed: {event.error}")
        messagebox.showerror("Ingest failed", event.error)

    def _reset_controls(self) -> None:
        self.run_button.config(state="normal" if self.tesseract_ok else "disabled")
        self.stop_button.config(state="disabled")
        self.job = None

    def clear_search(self) -> None:
        self.query.set("")
        self.refresh()

    def export_csv(self) -> None:
        if not self.db_path.exists():
            messagebox.showwarning("No index", "Nothing to export yet - run OCR first.")
            return
        target = filedialog.asksaveasfilename(
            title="Export extracted fields",
            defaultextension=".csv",
            initialfile="extracted.csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not target:
            return

        import csv as _csv

        from ..cli import EXPORT_COLUMNS

        with TessyIndex(self.db_path) as index:
            docs = index.all_documents()
        with open(target, "w", newline="") as handle:
            writer = _csv.DictWriter(handle, fieldnames=list(EXPORT_COLUMNS), extrasaction="ignore")
            writer.writeheader()
            for doc in docs:
                writer.writerow({k: doc.get(k) for k in EXPORT_COLUMNS})
        self.set_status(f"Exported {len(docs)} row(s) to {target}")

    # -- data --------------------------------------------------------------
    def refresh(self) -> None:
        """Reload the results list from the index."""
        self.tree.delete(*self.tree.get_children())
        self._rows.clear()

        if not self.db_path.exists():
            self.set_status(f"No index yet at {self.db_path} - choose a spreadsheet and run OCR.")
            return

        query = self.query.get().strip()
        try:
            with TessyIndex(self.db_path) as index:
                if self.view.get() == "review":
                    docs = index.needs_review()
                    if query:
                        wanted = {h.id for h in index.search(query, limit=500)}
                        docs = [d for d in docs if d["id"] in wanted]
                elif query:
                    docs = [index.get(h.id) for h in index.search(query, limit=500)]
                else:
                    docs = index.all_documents()
        except ValueError as exc:
            self.set_status(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a broken index must not kill the app
            self.set_status(f"Could not read the index: {exc}")
            return

        for doc in docs:
            if not doc:
                continue
            warnings = _warnings_of(doc)
            conf = doc.get("ocr_confidence")
            item = self.tree.insert(
                "",
                "end",
                values=(
                    doc.get("full_name") or "(name not parsed)",
                    doc.get("licence_no") or "-",
                    doc.get("dob") or "-",
                    f"{doc.get('sheet') or ''} {doc.get('row') or ''}".strip(),
                    f"{conf:.0f}" if isinstance(conf, (int, float)) else "-",
                ),
                tags=("flagged",) if warnings else (),
            )
            self._rows[item] = doc

        noun = "record" if len(self._rows) == 1 else "records"
        scope = "flagged" if self.view.get() == "review" else "indexed"
        matching = f" matching {query!r}" if query else ""
        self.set_status(f"{len(self._rows)} {scope} {noun}{matching}")

    def on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        doc = self._rows.get(selection[0])
        if doc:
            self._show_detail(doc)

    def _show_detail(self, doc: dict) -> None:
        self._show_preview(doc.get("image_path"))

        self.detail.config(state="normal")
        self.detail.delete("1.0", "end")

        warnings = _warnings_of(doc)
        if warnings:
            self.detail.insert("end", "Needs checking\n", "heading")
            for warning in warnings:
                self.detail.insert("end", f"  ! {warning}\n", "warn")
            self.detail.insert("end", "\n")

        for key, label in DETAIL_FIELDS:
            value = doc.get(key)
            self.detail.insert("end", f"{label:<14}", "label")
            if value:
                self.detail.insert("end", f"{value}\n", "value")
            else:
                self.detail.insert("end", "-\n", "missing")

        conf = doc.get("ocr_confidence")
        self.detail.insert("end", "\nSource\n", "heading")
        for label, value in (
            ("Spreadsheet", Path(doc["source"]).name if doc.get("source") else "-"),
            ("Sheet / row", f"{doc.get('sheet')} / {doc.get('row')}"),
            ("OCR conf", f"{conf:.1f}" if isinstance(conf, (int, float)) else "-"),
            ("PSM", doc.get("ocr_psm") or "-"),
            ("Image", doc.get("image_path") or "(text-only row)"),
        ):
            self.detail.insert("end", f"{label:<14}", "label")
            self.detail.insert("end", f"{value}\n")

        if doc.get("ocr_text"):
            self.detail.insert("end", "\nRaw OCR text\n", "heading")
            self.detail.insert("end", doc["ocr_text"] + "\n")

        self.detail.config(state="disabled")

    def _show_preview(self, image_path: str | None) -> None:
        if not image_path or not Path(image_path).is_file():
            self._preview_image = None
            self.preview.config(image="", text="(no image for this row)")
            return
        try:
            from PIL import Image, ImageTk

            with Image.open(image_path) as img:
                img = img.convert("RGB")
                img.thumbnail(PREVIEW_MAX, Image.LANCZOS)
                # Kept on self: tkinter holds only a weak reference and the
                # image would otherwise be collected and render blank.
                self._preview_image = ImageTk.PhotoImage(img)
            self.preview.config(image=self._preview_image, text="")
        except Exception as exc:  # noqa: BLE001 - a bad scan must not kill the view
            self._preview_image = None
            self.preview.config(image="", text=f"(could not display image: {exc})")

    # -- misc --------------------------------------------------------------
    def set_status(self, text: str) -> None:
        self.status.config(text=text)

    def on_close(self) -> None:
        if self.job is not None and self.job.running:
            if not messagebox.askokcancel("Quit", "OCR is still running. Stop it and quit?"):
                return
            self.job.cancel()
            self.job.join(timeout=5)
        self.master.destroy()


def _warnings_of(doc: dict) -> list[str]:
    raw = doc.get("warnings")
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


DOCTOR_HELP = """Tessy desktop app

Usage:
  Tessy [INDEX.db]     open the app, optionally on a specific index
  Tessy --doctor       print diagnostics and exit (no window)
  Tessy --selftest     prove OCR actually works end to end, and exit
  Tessy --version      print the version and exit
"""

SELFTEST_TEXT = "TESSY SELFTEST 12345"


def run_doctor() -> int:
    """Print diagnostics without opening a window.

    Exists mainly for the packaged app: when something is wrong on a machine we
    cannot inspect, this is the one command that can be asked for over a phone.
    """
    from ..bundle import describe
    from ..ocr import TesseractNotFound, available_languages, find_tesseract, tesseract_version

    print(f"Tessy {__version__}")
    for key, value in describe().items():
        print(f"  {key:<18}: {value or '-'}")

    try:
        print(f"  tesseract binary  : {find_tesseract()}")
        print(f"  tesseract version : {tesseract_version()}")
        langs = available_languages()
        print(f"  languages         : {', '.join(langs) if langs else 'NONE'}")
        if not langs:
            print("\nNo language data: OCR will fail. Reinstall or set TESSDATA_PREFIX.")
            return 1
    except TesseractNotFound as exc:
        print(f"  tesseract         : NOT FOUND ({exc})")
        return 1

    try:
        import tkinter

        print(f"  tkinter           : {tkinter.TkVersion}")
    except ImportError:
        print("  tkinter           : MISSING")
        return 1

    print("\nAll checks passed.")
    return 0


def run_selftest() -> int:
    """Render text, OCR it, and check it comes back.

    `--doctor` only proves the pieces are present. This proves they work
    together: in a packaged app it exercises the bundled Tesseract, the bundled
    language data and the bundled Pillow in one go, on a machine where nothing
    else is installed.
    """
    import os
    import tempfile

    from PIL import Image, ImageDraw, ImageFont

    from ..ocr import run_best

    # Set TESSY_SELFTEST_DIR to keep the probe image for inspection when
    # diagnosing a packaged build.
    keep_dir = os.environ.get("TESSY_SELFTEST_DIR")

    font = None
    for path in (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
    ):
        try:
            font = ImageFont.truetype(path, 56)
            break
        except (OSError, ImportError):
            continue
    if font is None:
        font = ImageFont.load_default()
        print("  font     : WARNING - no TrueType font found, using the bitmap default")
    else:
        print(f"  font     : {getattr(font, 'path', '(default)')}")

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(keep_dir or tmp) / "selftest.png"
        probe.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (760, 130), "white")
        ImageDraw.Draw(image).text((20, 30), SELFTEST_TEXT, font=font, fill="black")
        image.save(probe)

        try:
            result = run_best(probe)
        except Exception as exc:  # noqa: BLE001 - the whole point is to report it
            print(f"SELFTEST FAILED: OCR raised {exc.__class__.__name__}: {exc}")
            return 1

    got = " ".join(result.text.split())
    print(f"  rendered : {SELFTEST_TEXT}")
    print(f"  OCR read : {got}")
    print(f"  psm      : {result.psm}   confidence: {result.mean_confidence:.1f}")

    if got.upper() != SELFTEST_TEXT:
        print("\nSELFTEST FAILED: the text did not round-trip exactly.")
        return 1
    print("\nSelf-test passed: OCR is working.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `tessy-gui` command."""
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--doctor" in argv:
        return run_doctor()
    if "--selftest" in argv:
        return run_selftest()
    if "--version" in argv:
        print(f"tessy {__version__}")
        return 0
    if {"-h", "--help"} & set(argv):
        print(DOCTOR_HELP)
        return 0

    db = argv[0] if argv else "data/output/case_index.db"

    root = tk.Tk()
    with contextlib.suppress(tk.TclError):
        ttk.Style().theme_use("clam")  # consistent across platforms
    app = TessyApp(root, db_path=db)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
