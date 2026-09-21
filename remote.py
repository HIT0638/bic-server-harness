#!/usr/bin/env python3
"""Entry point: python remote.py <command> ... (see `python remote.py -h`)."""

import sys

from sshbridge.cli import main

if __name__ == "__main__":
    sys.exit(main())
