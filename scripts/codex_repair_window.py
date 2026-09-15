#!/usr/bin/env python3
"""Legacy Codex entry: implementation lives in spellguard.hook_core (D01).
Kept for compatibility with existing manual hook installs."""

import sys

from spellguard.hook_core import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
