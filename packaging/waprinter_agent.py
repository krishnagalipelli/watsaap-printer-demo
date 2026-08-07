"""PyInstaller entry point for the desktop agent.

Do not point PyInstaller at `src/waprinter/agent.py` directly. PyInstaller runs
its entry script as `__main__`, and a module inside a package cannot use
relative imports when run that way — `from .capture.spooler import ...` raises

    ImportError: attempted relative import with no known parent package

on the first line. Because the agent is frozen with `--windowed` there is no
console, so that traceback goes nowhere: the executable appears to do nothing at
all when double-clicked. This shim exists so the real module is imported as part
of its package, the normal way.
"""

from __future__ import annotations

import sys

from waprinter.agent import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
