#!/usr/bin/env python3
"""Entry point used by install.sh (symlinked as ~/.local/bin/polarum)."""
from __future__ import annotations

from polarum.cli import main

raise SystemExit(main())
