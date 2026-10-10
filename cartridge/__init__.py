"""Cartridge — local desktop game collection manager.

Fixed technical baseline: Python 3.8.x + PyQt5 + httpx + SQLite.

This package targets Windows 7 SP1 x64 on constrained hardware
(Dell Latitude E5500: Core 2 Duo, 2 GB RAM, GMA 4500MHD, 1280x800, HDD).
Everything here is written to be Python 3.8 compatible; `vermin` is used in CI
and by `scripts/check_py38.py` to prove it.
"""

__version__ = "0.1.2"
__app_name__ = "Cartridge"
__app_id__ = "cartridge"

# Bumped whenever the on-disk schema changes. See cartridge/db/schema.py.
SCHEMA_VERSION = 1
