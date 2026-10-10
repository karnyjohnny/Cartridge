#!/usr/bin/env python
"""Cartridge — single entry point.

    python run.py                     launch the application
    python run.py --selftest          build + render the UI headlessly, exit 0/1
    python run.py --root E:\\Gry       open a specific games root
    python run.py --db path\\to.db     open a specific database
    python run.py --offline           never contact a metadata provider
    python run.py --first-run         force the setup dialog
    python run.py --version           print the version

This file stays trivial on purpose: everything lives in ``cartridge/`` so it can
be imported and tested without starting an event loop. The same module is what
PyInstaller freezes (see ``packaging/cartridge.spec``).
"""

from __future__ import annotations

import os
import sys

# Make the repository importable when run from a source checkout
# (python run.py), which is the documented workflow on the Dell.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def main(argv=None) -> int:
    from cartridge.app import main as app_main

    return app_main(argv)


if __name__ == "__main__":
    sys.exit(main())
