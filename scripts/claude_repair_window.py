#!/usr/bin/env python3
"""Legacy thin wrapper: implementation lives in spellguard.claude_hook (D01)."""

import sys

from spellguard.claude_hook import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
