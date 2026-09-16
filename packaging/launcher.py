"""Entry point for the frozen desktop app.

PyInstaller executes its entry script as ``__main__``, which has no parent
package, so pointing it straight at ``tessy/gui/app.py`` makes every relative
import in the package fail at startup. Going through this launcher keeps the
app a normal importable package.
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    # Guards against a frozen app re-launching itself if any dependency ever
    # spawns a process; harmless otherwise.
    multiprocessing.freeze_support()

    from tessy.gui.app import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
