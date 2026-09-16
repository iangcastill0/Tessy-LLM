"""Desktop front end for Tessy.

Split in two so the logic stays testable without a display:

* ``jobs`` - background work and event plumbing, no tkinter import;
* ``app``  - the tkinter widgets.
"""

from __future__ import annotations

__all__ = ["IngestJob", "main"]


def __getattr__(name: str):
    if name == "IngestJob":
        from .jobs import IngestJob

        return IngestJob
    if name == "main":
        # Imported lazily: tkinter may be absent, and merely importing the
        # package (as the test suite does) must not fail because of that.
        from .app import main

        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
