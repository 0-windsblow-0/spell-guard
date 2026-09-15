#!/usr/bin/env python3
"""Legacy thin wrapper: implementation lives in spellguard.demo (D01)."""

import sys

from spellguard.demo import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
