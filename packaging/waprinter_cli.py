"""PyInstaller entry point for the command line tool.

See packaging/waprinter_agent.py for why the frozen entry point is a shim rather
than the module itself.
"""

from __future__ import annotations

import sys

from waprinter.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
